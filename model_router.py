"""Task-based routing for A.U.R.A.'s one-shot Gemini generation calls."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from PyQt6.QtCore import QObject, pyqtSignal

LOGGER = logging.getLogger(__name__)

TIERS: dict[str, tuple[str, ...]] = {
    "fast": (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
    ),
    "smart": (
        "gemini-3-flash-preview",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ),
    "deep": (
        "gemini-4-hydrogen",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-3.5-flash-lite",
    ),
}

# These standard inference models have a published Free Tier. Deep requests
# never fall through to a model outside this reviewed set.
_FREE_DEEP_MODELS = frozenset({
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
})

DEEP_TASK_TYPES = frozenset({
    "cad_design",
    "vibe_coding",
    "coding",
    "code",
    "multi_file_refactor",
    "long_agent_plan",
    "long_document_generation",
})

_CHEAP_TASK_TYPES = frozenset({
    "chat",
    "qa",
    "intent_parsing",
    "command_parsing",
    "reminder",
    "memory_lookup",
    "short_summary",
    "status",
    "tool_output_parsing",
    "formatting",
})
_MODEL_RPM = {
    "gemini-4-hydrogen": 5.0,
    "gemini-3.8-flash": 10.0,
    "gemini-3.6-flash": 15.0,
    "gemini-3.5-flash-lite": 15.0,
    "gemini-3.1-flash-lite": 15.0,
    "gemini-2.5-flash-lite": 15.0,
    "gemini-2.5-flash": 15.0,
}
_MODEL_DAILY_FREE_LIMITS = {
    "gemini-4-hydrogen": 50,
    "gemini-3.8-flash": 50,
    "gemini-3.6-flash": 50,
    "gemini-3.5-flash-lite": 1500,
}
_MODEL_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_MODEL_REFRESH_SECONDS = 6 * 60 * 60
_COOLDOWN_DEFAULT = 60.0
_COOLDOWN_MAX = 15 * 60.0
_NOTICE_INTERVAL = 30.0
_CONTINUE_INSTRUCTION = (
    "Continue the existing response exactly where it stopped. Do not repeat "
    "any text already present in the partial response."
)
_MODEL_SPECIFIC_FIELDS = frozenset({
    "thought_signature",
    "thoughtSignature",
    "thinking",
    "thoughts",
    "model",
    "model_version",
    "modelVersion",
    "metadata",
})


class RouterError(RuntimeError):
    """A safe, user-facing description of a routing failure."""


class InvalidApiKeyError(RouterError):
    """The configured Gemini key was rejected and must not be retried."""


class RetryLaterError(RouterError):
    """All eligible models are cooling down; the session remains resumable."""


class RouterSignals(QObject):
    """Thread-safe notifications for UI consumers; this module has no UI imports."""

    model_switched = pyqtSignal(str)
    invalid_api_key = pyqtSignal()


@dataclass
class ToolCallState:
    """A tool call's idempotency state within one task session."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    completed: bool = False
    in_progress: bool = False
    result: Any = None


@dataclass
class TaskSession:
    """Context retained across retries and model handoffs."""

    task_type: str
    system_prompt: str = ""
    messages: list[Any] = field(default_factory=list)
    tool_call_log: list[ToolCallState] = field(default_factory=list)
    partial_output: str = ""
    current_model: str | None = None
    task_id: str = field(default_factory=lambda: uuid4().hex)
    pending: bool = False
    latest_result: Any = None

    def claim_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: dict,
        *,
        replay_unfinished: bool = False,
    ) -> bool:
        """Claim a tool action once; completed or active calls cannot replay."""
        for record in self.tool_call_log:
            if record.call_id == call_id:
                if replay_unfinished and not record.completed:
                    record.in_progress = True
                    return True
                return False
        self.tool_call_log.append(ToolCallState(
            call_id=call_id,
            name=name,
            arguments=dict(arguments),
            in_progress=True,
        ))
        return True

    def complete_tool_call(self, call_id: str, result: Any) -> None:
        """Record a tool result before the next model request."""
        for record in self.tool_call_log:
            if record.call_id == call_id:
                record.in_progress = False
                record.completed = True
                record.result = result
                return
        raise KeyError(f"Unknown tool call: {call_id}")

    def safe_messages(self) -> list[Any]:
        """Copy history while stripping model-specific response fields."""
        return [_strip_model_fields(message) for message in self.messages]


@dataclass(frozen=True)
class GenerationMetadata:
    model_used: str
    switched: bool
    previous_model: str | None
    reason: str
    tier: str


@dataclass
class GenerationResponse:
    """A response compatible with the `.text` access used by existing callers."""

    text: str
    raw: Any = None
    model_used: str | None = None
    previous_model: str | None = None
    reason: str = "task"
    tier: str | None = None

    def __getattr__(self, name: str) -> Any:
        if self.raw is None:
            raise AttributeError(name)
        return getattr(self.raw, name)


class TokenBucket:
    """Thread-safe token bucket with bounded waiting."""

    def __init__(self, requests_per_minute: float = 5.0) -> None:
        self._lock = threading.Condition()
        self.configure(requests_per_minute)
        self._tokens = self.capacity
        self._updated = time.monotonic()

    def configure(self, requests_per_minute: float) -> None:
        rate = float(requests_per_minute)
        if rate <= 0:
            raise ValueError("requests_per_minute must be greater than zero")
        with getattr(self, "_lock", threading.Condition()):
            self.capacity = rate
            self.refill_per_second = rate / 60.0
            if hasattr(self, "_tokens"):
                self._tokens = min(self._tokens, self.capacity)

    def acquire(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            while True:
                now = time.monotonic()
                elapsed = max(0.0, now - self._updated)
                self._tokens = min(
                    self.capacity, self._tokens + elapsed * self.refill_per_second
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                remaining = deadline - now
                if remaining <= 0:
                    return False
                self._lock.wait(min(remaining, (1.0 - self._tokens) / self.refill_per_second))


def _strip_model_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_model_fields(item)
            for key, item in value.items()
            if key not in _MODEL_SPECIFIC_FIELDS
        }
    for method_name in ("model_dump", "dict"):
        dumper = getattr(value, method_name, None)
        if callable(dumper):
            return _strip_model_fields(dumper())
    if isinstance(value, list):
        return [_strip_model_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_model_fields(item) for item in value)
    return value


def classify_task(
    task_type: str | None,
    text: Any = "",
    context: Any = None,
) -> str:
    """Choose a tier with explicit task types taking priority over heuristics."""
    normalized = str(task_type or "").strip().casefold()
    if normalized in DEEP_TASK_TYPES:
        return "deep"
    if normalized in _CHEAP_TASK_TYPES:
        return "fast"
    if normalized in {"debugging", "file_task", "ide_task", "long_reasoning"}:
        return "smart"

    sample = " ".join(
        str(part) for part in (text, context) if part is not None
    ).casefold()
    if not sample:
        return "smart"
    if any(word in sample for word in (
        "cad design", "design a cad", "cad designing", "cad model", "3d cad",
        "vibe coding", "coding task", "coding", "write code", "generate code",
        "build a full app", "build an app", "create a website", "build a website",
        "multi-file refactor", "long agent plan", "write a long document",
        "generate a long document", "draft a long document",
    )):
        return "deep"
    if any(word in sample for word in (
        "debug", "refactor", "ide task",
        "multiple steps", "step-by-step", "long reasoning", "large context",
        "analyze this file", "write a report",
    )):
        return "smart"
    if len(sample) <= 500:
        return "fast"
    return "smart"


class ModelRouter:
    """Route one-shot generations across free Gemini tiers and local fallback."""

    def __init__(
        self,
        *,
        key_provider: Callable[[], str] | None = None,
        client_factory: Callable[[str, int], Any] | None = None,
        model_lister: Callable[[str], set[str]] | None = None,
        settings_provider: Callable[[], dict] | None = None,
        local_fallback: Callable[[str, str, int], str] | None = None,
        legacy_fallback: Callable[[Any, Any, int, str, str], Any] | None = None,
        budget_file: str | Path | None = None,
        requests_per_minute: float = 5.0,
        notice_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.signals = RouterSignals()
        self._key_provider = key_provider or self._default_key
        self._client_factory = client_factory or self._default_client
        self._model_lister = model_lister or self._list_models
        self._settings_provider = settings_provider or self._default_settings
        self._local_fallback = local_fallback or self._default_local_fallback
        self._legacy_fallback = legacy_fallback or self._default_legacy_fallback
        self._budget_file = Path(budget_file) if budget_file else (
            Path(__file__).resolve().parent / "config" / "model_router_budget.json"
        )
        self.requests_per_minute = float(requests_per_minute)
        self._notice_clock = notice_clock
        self._sleep = sleeper
        self._lock = threading.RLock()
        self._models: set[str] | None = None
        self._models_key_id: str | None = None
        self._last_refresh = 0.0
        self._refresh_running = False
        self._last_model: str | None = None
        self._last_notice: float | None = None
        self._pending_notice: str | None = None
        self._notice_timer: threading.Timer | None = None
        self._cooldowns: dict[tuple[str, str], tuple[float, int]] = {}
        self._cooldown_counts: dict[tuple[str, str], int] = {}
        self._disabled: dict[str, set[str]] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._daily_counts: dict[str, dict[str, int]] = {}
        self._pending_sessions: dict[str, TaskSession] = {}
        self._load_budgets()

    @staticmethod
    def _default_key() -> str:
        from memory.config_manager import get_gemini_key

        return str(get_gemini_key() or "")

    @staticmethod
    def _default_settings() -> dict:
        from memory.config_manager import get_local_llm_config, load_api_keys

        api_config = load_api_keys()
        local = get_local_llm_config()
        if isinstance(api_config, dict):
            local["auto_model_switching"] = bool(
                api_config.get("local_llm", {}).get("auto_model_switching", True)
                if isinstance(api_config.get("local_llm"), dict) else True
            )
        return local

    @staticmethod
    def _default_client(key: str, timeout_ms: int):
        from google import genai
        from google.genai import types

        return genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=max(10_000, int(timeout_ms))),
        )

    @staticmethod
    def _list_models(key: str) -> set[str]:
        found = set()
        page_token = ""
        seen_tokens = set()
        for _page in range(10):
            params = {"key": key, "pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            query = urllib.parse.urlencode(params)
            request = urllib.request.Request(
                f"{_MODEL_LIST_URL}?{query}",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10.0) as response:
                body = json.loads(response.read().decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Gemini returned an invalid model list.")
            for record in body.get("models", []):
                if not isinstance(record, dict):
                    continue
                name = record.get("name")
                methods = (
                    record.get("supportedGenerationMethods")
                    or record.get("supported_generation_methods")
                    or record.get("supportedActions")
                    or record.get("supported_actions")
                )
                if isinstance(name, str) and isinstance(methods, list):
                    if "generateContent" in methods:
                        found.add(name.removeprefix("models/"))
            next_token = body.get("nextPageToken")
            if (
                not isinstance(next_token, str)
                or not next_token
                or next_token in seen_tokens
            ):
                break
            seen_tokens.add(next_token)
            page_token = next_token
        return found

    @staticmethod
    def _default_local_fallback(prompt: str, system: str, timeout_ms: int) -> str:
        from core.llm_client import call_llm

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        result = call_llm(messages, timeout=max(1, timeout_ms // 1000))
        return str(result.get("content") or "")

    @staticmethod
    def _default_legacy_fallback(
        contents: Any,
        config: Any,
        timeout_ms: int,
        key: str,
        tier: str,
    ) -> Any:
        """Use the existing one-shot Live/text ladder as a final Gemini route."""
        from core import gemini

        legacy_tier = gemini.FAST if tier == "fast" else gemini.SMART
        return gemini._legacy_call(
            contents,
            tier=legacy_tier,
            config=config,
            timeout_ms=timeout_ms,
            key=key,
        )

    def auto_enabled(self) -> bool:
        try:
            return bool(self._settings_provider().get("auto_model_switching", True))
        except Exception:
            LOGGER.exception("Could not read model-router settings")
            return True

    def configure_rate_limit(self, requests_per_minute: float) -> None:
        rate = float(requests_per_minute)
        if rate <= 0:
            raise ValueError("requests_per_minute must be greater than zero")
        with self._lock:
            self.requests_per_minute = rate
            for bucket in self._buckets.values():
                bucket.configure(rate)

    def start_refresh(self) -> None:
        """Start model discovery off-thread and refresh it every six hours."""
        with self._lock:
            if self._refresh_running:
                return
            self._refresh_running = True

        def refresh_loop() -> None:
            try:
                while True:
                    try:
                        self.refresh_models()
                    except Exception:
                        LOGGER.warning("Gemini model discovery failed; static chains remain enabled")
                    time.sleep(_MODEL_REFRESH_SECONDS)
            finally:
                with self._lock:
                    self._refresh_running = False

        threading.Thread(
            target=refresh_loop, name="gemini-model-discovery", daemon=True
        ).start()

    def refresh_models(self) -> set[str]:
        """Refresh accessible generation models without retaining the API key."""
        key = self._key_provider()
        if not key:
            raise RouterError("Gemini is not configured.")
        models = self._model_lister(key)
        if not isinstance(models, set):
            models = set(models)
        with self._lock:
            self._models = {str(name).removeprefix("models/") for name in models}
            self._models_key_id = _key_id(key)
            self._last_refresh = time.monotonic()
            self._disabled.pop(_key_id(key), None)
            return set(self._models)

    def _ensure_models(self, key: str) -> set[str] | None:
        with self._lock:
            stale = (
                self._models is None
                or self._models_key_id != _key_id(key)
                or time.monotonic() - self._last_refresh >= _MODEL_REFRESH_SECONDS
            )
        if stale:
            try:
                models = self._model_lister(key)
                with self._lock:
                    self._models = {
                        str(name).removeprefix("models/") for name in models
                    }
                    self._models_key_id = _key_id(key)
                    self._last_refresh = time.monotonic()
                    self._disabled.pop(_key_id(key), None)
            except Exception:
                LOGGER.warning("Gemini model discovery failed; static chains remain enabled")
                with self._lock:
                    if self._models is None or self._models_key_id != _key_id(key):
                        return None
        with self._lock:
            return set(self._models) if self._models is not None else None

    def generate(
        self,
        task_type: str | None = None,
        prompt: Any = None,
        *,
        messages: list[Any] | None = None,
        context: Any = None,
        session: TaskSession | None = None,
        on_chunk: Callable[[str], None] | None = None,
        critical: bool = False,
        generator: Callable[[str, Any, Any, str, int], Any] | None = None,
        allow_local_fallback: bool = True,
        **kwargs: Any,
    ) -> tuple[Any, GenerationMetadata]:
        """Generate once, returning the SDK-style response and routing metadata."""
        auto_switching = self.auto_enabled()
        raw_contents = messages if messages is not None else kwargs.get("contents", prompt)
        requested_tier = classify_task(
            task_type or (session.task_type if session is not None else None),
            _text_contents(raw_contents),
            context,
        )
        if not auto_switching and requested_tier != "deep":
            return self._generate_legacy(
                task_type, prompt, messages, kwargs, generator=generator
            )
        if _is_qt_main_thread():
            raise RouterError("Gemini routing must run outside the Qt UI thread.")

        contents = kwargs.pop("contents", None)
        if messages is not None:
            supplied_messages = list(messages)
            system_messages = [
                message for message in supplied_messages
                if isinstance(message, dict) and message.get("role") == "system"
            ]
            if system_messages and not kwargs.get("system_instruction"):
                kwargs["system_instruction"] = "\n".join(
                    str(message.get("content") or "")
                    for message in system_messages
                )
            messages = [
                message for message in supplied_messages
                if not (
                    isinstance(message, dict)
                    and message.get("role") == "system"
                )
            ]
            contents = (
                _to_gemini_history(messages)
                if all(isinstance(message, dict) for message in messages)
                else messages
            )
        elif contents is None:
            contents = prompt
        if contents is None:
            raise ValueError("prompt, messages, or contents is required")
        task_prompt = _text_contents(contents)
        can_local_fallback = (
            allow_local_fallback and not _contains_nontext_content(contents)
        )
        config = kwargs.get("config")
        system_prompt = str(
            kwargs.pop("system_instruction", "")
            or _system_instruction_from_config(config)
            or ""
        )
        tier = classify_task(
            task_type or (session.task_type if session is not None else None),
            task_prompt,
            context,
        )
        session = session or TaskSession(
            task_type=task_type or tier,
            system_prompt=system_prompt,
        )
        if system_prompt:
            session.system_prompt = system_prompt
        if messages is not None:
            if not session.messages:
                session.messages.extend(messages)
            elif session.messages != messages:
                session.messages = list(messages)
        elif not session.messages or _text_contents(session.messages[-1]) != task_prompt:
            session.messages.append({"role": "user", "content": contents})
        timeout_ms = int(kwargs.pop("timeout_ms", 30_000))
        key = str(kwargs.pop("key", "") or self._key_provider())
        if not key:
            raise RouterError("Gemini is not configured.")
        key_id = _key_id(key)
        available = self._ensure_models(key)
        chains = _chains_for_tier(tier, available)
        if available is not None:
            chains = {
                name: tuple(model for model in chain if model in available)
                for name, chain in chains.items()
            }
        if tier == "deep":
            eligible = _FREE_DEEP_MODELS.intersection(available or ())
            chains = {
                name: tuple(model for model in chain if model in eligible)
                for name, chain in chains.items()
            }
            if available is None:
                chains = {name: () for name in TIERS}
        order = _fallback_tiers(tier)
        candidates: list[tuple[str, str]] = []
        seen_models: set[str] = set()
        task_label = str(task_type or (session.task_type if session is not None else "")).strip().casefold()
        is_critical = critical or (task_label in {"cad_design", "vibe_coding", "coding", "code"})
        budget_excluded = {
            model for model in chains.get("deep", ())
            if tier == "deep" and (
                self._budget_exhausted(model)
                or (not is_critical and self._budget_near_limit(model))
            )
        }
        with self._lock:
            disabled = self._disabled.setdefault(key_id, set())
        for chain_tier in order:
            for model in chains.get(chain_tier, ()):
                if (
                    model in disabled
                    or model in seen_models
                    or model in budget_excluded
                ):
                    continue
                candidates.append((chain_tier, model))
                seen_models.add(model)
        if not candidates:
            session.pending = True
            with self._lock:
                self._pending_sessions[session.task_id] = session
            local = (
                self._try_local_fallback(task_prompt, system_prompt, timeout_ms)
                if can_local_fallback else None
            )
            if local is not None:
                return self._finish(local, "local", tier, session, "unavailable")
            if tier != "deep":
                legacy = self._try_legacy_fallback(
                    contents, config, timeout_ms, key, tier, session, on_chunk
                )
                if legacy is not None:
                    return self._finish(legacy, "legacy", tier, session, "unavailable")
            raise RetryLaterError("Please try again in a moment.")

        previous = session.current_model or self._last_model
        failure_reason: str | None = None
        for chain_tier, model in candidates:
            if self._is_cooling(key_id, model):
                failure_reason = failure_reason or "rate_limit"
                continue
            if not self._acquire_token(key_id, model):
                session.pending = True
                with self._lock:
                    self._pending_sessions[session.task_id] = session
                local = (
                    self._try_local_fallback(task_prompt, system_prompt, timeout_ms)
                    if can_local_fallback else None
                )
                if local is not None:
                    return self._finish(local, "local", tier, session, "rate_limit")
                raise RetryLaterError("Please try again in a moment.")
            try:
                config = _merge_system_instruction(
                    kwargs.get("config"), session.system_prompt
                )
                if generator is not None:
                    response = generator(model, contents, config, key, timeout_ms)
                else:
                    client = self._client_factory(key, timeout_ms)
                    if on_chunk is not None:
                        response = self._generate_stream(
                            client, model, contents, config, session, on_chunk,
                            key_id, key, candidates, chain_tier, timeout_ms,
                        )
                        model = response.model_used or model
                        chain_tier = response.tier or chain_tier
                        previous = response.previous_model or previous
                        failure_reason = (
                            None if response.reason == "task" else response.reason
                        )
                    else:
                        response = client.models.generate_content(
                            model=model,
                            contents=self._handoff_contents(contents, session)
                            if session.partial_output else contents,
                            **({"config": config} if config is not None else {}),
                        )
                if tier == "deep":
                    self._increment_budget(model)
                reason = failure_reason or getattr(response, "reason", "task")
                return self._finish(response, model, chain_tier, session, reason, previous)
            except Exception as error:
                if isinstance(error, RetryLaterError):
                    failure_reason = failure_reason or "unavailable"
                    break
                status = _status_code(error)
                category = _error_category(error, status)
                if category == "cancelled":
                    raise
                if category == "auth":
                    self.signals.invalid_api_key.emit()
                    raise InvalidApiKeyError(
                        "Gemini rejected the configured API key."
                    ) from error
                if category == "rate_limit":
                    self._cool_model(
                        key_id, model, _retry_after_seconds(error)
                    )
                    failure_reason = "rate_limit"
                elif category == "not_found":
                    with self._lock:
                        disabled.add(model)
                    failure_reason = failure_reason or "unavailable"
                else:
                    failure_reason = failure_reason or "unavailable"
                    if category in {"server", "timeout"}:
                        self._sleep(0.25)
                LOGGER.info("Gemini model %s unavailable (%s)", model, category)

        session.pending = True
        with self._lock:
            self._pending_sessions[session.task_id] = session
        local = (
            self._try_local_fallback(task_prompt, system_prompt, timeout_ms)
            if can_local_fallback else None
        )
        if local is not None:
            return self._finish(
                local, "local", tier, session, failure_reason or "unavailable", previous
            )
        if tier != "deep":
            legacy = self._try_legacy_fallback(
                contents,
                _merge_system_instruction(kwargs.get("config"), session.system_prompt),
                timeout_ms,
                key,
                tier,
                session,
                on_chunk,
            )
            if legacy is not None:
                return self._finish(
                    legacy, "legacy", tier, session, failure_reason or "unavailable", previous
                )
        raise RetryLaterError("Please try again in a moment.")

    def _generate_legacy(
        self,
        task_type: str | None,
        prompt: Any,
        messages: list[Any] | None,
        kwargs: dict,
        *,
        generator: Callable[[str, Any, Any, str, int], Any] | None = None,
    ) -> tuple[Any, GenerationMetadata]:
        """Auto-off path: retain the existing wrapper's chosen model behavior."""
        from core import gemini

        contents = messages if messages is not None else kwargs.pop("contents", prompt)
        tier = classify_task(task_type, _text_contents(contents))
        if generator is not None:
            model = str(kwargs.pop("legacy_tier", gemini.SMART))
            key = str(kwargs.pop("key", "") or self._key_provider())
            timeout_ms = int(kwargs.pop("timeout_ms", 30_000))
            response = generator(
                model, contents, kwargs.pop("config", None), key, timeout_ms
            )
            return response, GenerationMetadata(model, False, model, "task", tier)
        response = gemini._legacy_call(
            contents,
            tier=kwargs.pop("legacy_tier", gemini.SMART),
            config=kwargs.pop("config", None),
            timeout_ms=kwargs.pop("timeout_ms", 30_000),
            key=kwargs.pop("key", ""),
        )
        model = self._response_model(response) or "legacy"
        metadata = GenerationMetadata(model, False, model, "task", tier)
        return response, metadata

    def _generate_stream(
        self,
        client: Any,
        model: str,
        contents: Any,
        config: Any,
        session: TaskSession,
        on_chunk: Callable[[str], None],
        key_id: str,
        key: str,
        candidates: list[tuple[str, str]],
        tier: str,
        timeout_ms: int,
    ) -> GenerationResponse:
        parts: list[str] = []
        previous_output = session.partial_output
        previous_model = session.current_model
        failure_reason = "task"
        start = candidates.index((tier, model))
        for index in range(start, len(candidates)):
            active_tier, active_model = candidates[index]
            if index != start:
                if self._is_cooling(key_id, active_model):
                    failure_reason = "rate_limit"
                    continue
                if self._budget_exhausted(active_model):
                    continue
                if not self._acquire_token(key_id, active_model):
                    break
                client = self._client_factory(key, timeout_ms)
            try:
                stream = client.models.generate_content_stream(
                    model=active_model,
                    contents=self._handoff_contents(contents, session)
                    if session.partial_output else contents,
                    **({"config": config} if config is not None else {}),
                )
                for chunk in stream:
                    text = _extract_chunk_text(chunk)
                    if not text:
                        continue
                    text = _remove_duplicate_prefix(
                        previous_output + "".join(parts), str(text)
                    )
                    if text:
                        parts.append(text)
                        session.partial_output += text
                        on_chunk(text)
                session.current_model = active_model
                session.pending = False
                return GenerationResponse(
                    previous_output + "".join(parts),
                    model_used=active_model,
                    previous_model=previous_model or (
                        model if active_model != model else None
                    ),
                    reason=failure_reason,
                    tier=active_tier,
                )
            except Exception as error:
                status = _status_code(error)
                category = _error_category(error, status)
                if category == "cancelled":
                    raise
                if category == "auth":
                    self.signals.invalid_api_key.emit()
                    raise InvalidApiKeyError(
                        "Gemini rejected the configured API key."
                    ) from error
                if category == "rate_limit":
                    self._cool_model(key_id, active_model, _retry_after_seconds(error))
                    failure_reason = "rate_limit"
                elif category == "not_found":
                    with self._lock:
                        self._disabled.setdefault(key_id, set()).add(active_model)
                    failure_reason = "unavailable"
                elif category in {"server", "timeout"}:
                    self._sleep(0.25)
                    failure_reason = "unavailable"
                else:
                    failure_reason = "unavailable"
                if index + 1 >= len(candidates):
                    break
                session.current_model = active_model
                LOGGER.info("Gemini stream model %s unavailable (%s)", active_model, category)
        session.pending = True
        raise RetryLaterError("Please try again in a moment.")

    def _finish(
        self,
        response: Any,
        model: str,
        tier: str,
        session: TaskSession,
        reason: str,
        previous: str | None = None,
    ) -> tuple[Any, GenerationMetadata]:
        prior = previous if previous is not None else self._last_model
        switched = prior is not None and prior != model
        session.current_model = model
        session.pending = False
        session.partial_output = ""
        session.latest_result = response
        response_text = getattr(response, "text", None)
        if isinstance(response_text, str) and response_text:
            session.messages.append({"role": "model", "content": response_text})
        with self._lock:
            self._pending_sessions.pop(session.task_id, None)
            self._last_model = model
        if switched:
            self._queue_notice(_notice_text(reason, tier))
        return response, GenerationMetadata(
            model_used=model,
            switched=switched,
            previous_model=prior,
            reason=reason,
            tier=tier,
        )

    def _queue_notice(self, message: str) -> None:
        now = self._notice_clock()
        with self._lock:
            elapsed = (
                None if self._last_notice is None else now - self._last_notice
            )
            if elapsed is None or elapsed >= _NOTICE_INTERVAL:
                self._last_notice = now
                self._pending_notice = None
                self.signals.model_switched.emit(message)
                return
            self._pending_notice = message
            if self._notice_timer is not None:
                self._notice_timer.cancel()
            self._notice_timer = threading.Timer(
                max(0.01, _NOTICE_INTERVAL - float(elapsed)), self._deliver_notice
            )
            self._notice_timer.daemon = True
            self._notice_timer.start()

    def _deliver_notice(self) -> None:
        with self._lock:
            message = self._pending_notice
            self._pending_notice = None
            self._notice_timer = None
            if message is None:
                return
            self._last_notice = self._notice_clock()
        self.signals.model_switched.emit(message)

    def _rpm_for_model(self, model: str | None) -> float:
        if self.requests_per_minute != 5.0:
            return self.requests_per_minute
        if model and model in _MODEL_RPM:
            return float(_MODEL_RPM[model])
        return self.requests_per_minute

    def _acquire_token(self, key_id: str, model: str | None = None) -> bool:
        bucket_key = f"{key_id}:{model}" if model else key_id
        rpm = self._rpm_for_model(model)
        with self._lock:
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                bucket = TokenBucket(rpm)
                self._buckets[bucket_key] = bucket
        if _is_qt_main_thread():
            return bucket.acquire(timeout=0.0)
        return bucket.acquire(timeout=15.0)

    def _is_cooling(self, key_id: str, model: str) -> bool:
        return self._remaining_cooldown(key_id, model) > 0.0

    def _remaining_cooldown(self, key_id: str, model: str) -> float:
        with self._lock:
            until, _count = self._cooldowns.get((key_id, model), (0.0, 0))
            remaining = until - time.monotonic()
            if remaining <= 0:
                self._cooldowns.pop((key_id, model), None)
                return 0.0
            return remaining

    def _cool_model(self, key_id: str, model: str, retry_after: float | None) -> None:
        with self._lock:
            cooldown_key = (key_id, model)
            count = self._cooldown_counts.get(cooldown_key, 0) + 1
            self._cooldown_counts[cooldown_key] = count
            delay = retry_after or min(
                _COOLDOWN_DEFAULT * (2 ** min(count - 1, 4)),
                _COOLDOWN_MAX,
            )
            delay = min(max(1.0, delay), _COOLDOWN_MAX)
            self._cooldowns[(key_id, model)] = (time.monotonic() + delay, count)

    def _try_local_fallback(
        self, prompt: str, system: str, timeout_ms: int
    ) -> GenerationResponse | None:
        try:
            settings = self._settings_provider()
        except Exception:
            LOGGER.exception("Could not read local fallback settings")
            return None
        if settings.get("mode") != "fallback" or not prompt:
            return None
        try:
            text = self._local_fallback(prompt, system, timeout_ms)
            if not str(text or "").strip():
                return None
            return GenerationResponse(str(text))
        except Exception:
            LOGGER.exception("Configured local model fallback failed")
            return None

    def _try_legacy_fallback(
        self,
        contents: Any,
        config: Any,
        timeout_ms: int,
        key: str,
        tier: str,
        session: TaskSession,
        on_chunk: Callable[[str], None] | None,
    ) -> GenerationResponse | None:
        fallback_contents = (
            self._handoff_contents(contents, session)
            if session.partial_output else contents
        )
        try:
            response = self._legacy_fallback(
                fallback_contents, config, timeout_ms, key, tier
            )
        except Exception as error:
            LOGGER.info(
                "Final Gemini fallback unavailable (%s)", type(error).__name__
            )
            return None
        text = str(getattr(response, "text", "") or "")
        if not text.strip():
            return None
        added_text = _remove_duplicate_prefix(session.partial_output, text)
        if on_chunk is not None and added_text:
            on_chunk(added_text)
        full_text = session.partial_output + added_text
        session.partial_output = full_text
        return GenerationResponse(
            full_text,
            raw=response,
            model_used="legacy",
            previous_model=session.current_model,
            reason="unavailable",
            tier=tier,
        )

    def _load_budgets(self) -> None:
        try:
            data = json.loads(self._budget_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._daily_counts = {
                    str(day): {
                        str(model): max(0, int(count))
                        for model, count in models.items()
                    }
                    for day, models in data.items()
                    if isinstance(models, dict)
                }
        except FileNotFoundError:
            self._daily_counts = {}
        except (OSError, ValueError):
            LOGGER.warning("Could not load local deep-tier budget data")

    def _daily_limit_for_model(self, model: str) -> int:
        try:
            budget = int(self._settings_provider().get("deep_daily_budget", 50))
        except (AttributeError, TypeError, ValueError):
            budget = 50
        model_cap = _MODEL_DAILY_FREE_LIMITS.get(model, 50)
        return min(budget, model_cap) if budget > 0 else model_cap

    def _budget_exhausted(self, model: str) -> bool:
        limit = self._daily_limit_for_model(model)
        if limit <= 0:
            return False
        today_counts = self._daily_counts.get(date.today().isoformat(), {})
        count = int(today_counts.get(model, 0))
        return count >= limit

    def _budget_near_limit(self, model: str) -> bool:
        limit = self._daily_limit_for_model(model)
        if limit <= 0:
            return False
        today_counts = self._daily_counts.get(date.today().isoformat(), {})
        count = int(today_counts.get(model, 0))
        return count >= max(1, math.ceil(limit * 0.8))

    def _increment_budget(self, model: str) -> None:
        today = date.today().isoformat()
        with self._lock:
            counts = self._daily_counts.setdefault(today, {})
            counts[model] = int(counts.get(model, 0)) + 1
            self._daily_counts = {
                day: values for day, values in self._daily_counts.items()
                if day == today
            }
            try:
                self._budget_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._budget_file.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(self._daily_counts, sort_keys=True),
                    encoding="utf-8",
                )
                temporary.replace(self._budget_file)
            except OSError:
                LOGGER.exception("Could not persist deep-tier budget data")

    @staticmethod
    def _response_model(response: Any) -> str | None:
        return getattr(response, "model_version", None)

    @staticmethod
    def _handoff_contents(contents: Any, session: TaskSession) -> Any:
        if not session.partial_output:
            return contents
        tool_history = "\n".join(
            f"Tool {record.name} completed; result: {record.result}"
            if record.completed else
            f"Tool {record.name} is in progress and must not be re-executed."
            for record in session.tool_call_log
        )
        continuation = (
            f"{_CONTINUE_INSTRUCTION}\n"
            + (f"Original system instruction:\n{session.system_prompt}\n" if session.system_prompt else "")
            + (f"Completed tool-call history:\n{tool_history}\n" if tool_history else "")
            + f"Partial output so far:\n{session.partial_output}"
        )
        history = session.safe_messages()
        if len(history) > 1:
            return _to_gemini_history(history) + [
                {"role": "user", "parts": [{"text": continuation}]}
            ]
        if isinstance(contents, str):
            return f"{contents}\n\n{continuation}"
        if isinstance(contents, list):
            return list(contents) + [continuation]
        return [contents, continuation]


def _merge_system_instruction(config: Any, system_prompt: str) -> Any:
    if not system_prompt:
        return config
    if config is None:
        return {"system_instruction": system_prompt}
    if isinstance(config, dict):
        merged = dict(config)
        merged["system_instruction"] = system_prompt
        return merged
    copier = getattr(config, "model_copy", None)
    if callable(copier):
        return copier(update={"system_instruction": system_prompt})
    return config


def _system_instruction_from_config(config: Any) -> Any:
    if isinstance(config, dict):
        return config.get("system_instruction")
    return getattr(config, "system_instruction", None)


def _to_gemini_history(messages: list[Any]) -> list[Any]:
    result = []
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("parts"), list):
            result.append(message)
            continue
        if isinstance(message, dict):
            role = str(message.get("role") or "user")
            role = "model" if role == "assistant" else role
            text = message.get("content")
            if isinstance(text, list):
                parts = text
            else:
                parts = [{"text": str(text or "")}]
            result.append({"role": role if role in {"user", "model"} else "user", "parts": parts})
            continue
        result.append({"role": "user", "parts": [{"text": str(message)}]})
    return result


def _key_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _text_contents(contents: Any) -> str:
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        if isinstance(contents.get("text"), str):
            return contents["text"]
        if isinstance(contents.get("content"), str):
            return contents["content"]
        if isinstance(contents.get("parts"), list):
            return _text_contents(contents["parts"])
    if isinstance(contents, (list, tuple)):
        values = []
        for item in contents:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                values.append(str(
                    item.get("text") or item.get("content")
                    or _text_contents(item.get("parts", []))
                ))
            else:
                values.append(str(getattr(item, "text", "") or ""))
        return "\n".join(values)
    return str(contents or "")


def _contains_nontext_content(contents: Any) -> bool:
    if isinstance(contents, dict):
        if isinstance(contents.get("text"), str) or isinstance(contents.get("content"), str):
            return False
        parts = contents.get("parts")
        return not isinstance(parts, list) or _contains_nontext_content(parts)
    if isinstance(contents, (list, tuple)):
        for item in contents:
            if isinstance(item, str):
                continue
            if isinstance(item, dict):
                if isinstance(item.get("text"), str) or isinstance(item.get("content"), str):
                    continue
                if isinstance(item.get("parts"), list) and not _contains_nontext_content(
                    item["parts"]
                ):
                    continue
                return True
            if isinstance(getattr(item, "text", None), str):
                continue
            return True
    elif not isinstance(contents, (str, dict)) and contents is not None:
        return True
    return False


def _chains_for_tier(
    tier: str, available: set[str] | None
) -> dict[str, tuple[str, ...]]:
    if available is None:
        return dict(TIERS)
    return {
        name: tuple(model for model in chain if model in available)
        for name, chain in TIERS.items()
    }


def _fallback_tiers(tier: str) -> tuple[str, ...]:
    if tier == "deep":
        return ("deep", "smart", "fast")
    if tier == "smart":
        return ("smart", "fast")
    return ("fast", "smart")


def _status_code(error: Exception) -> int | None:
    for candidate in (
        getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
        getattr(error, "code", None),
    ):
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            continue
    match = re.search(r"\b(401|403|404|429|5\d\d)\b", str(error))
    return int(match.group(1)) if match else None


def _error_category(error: Exception, status: int | None) -> str:
    message = str(error).casefold()
    response = getattr(error, "response", None)
    body = getattr(response, "text", None)
    if isinstance(body, str):
        message += f" {body.casefold()}"
    if "screen agent was stopped" in message:
        return "cancelled"
    if status in {401, 403} or any(
        marker in message for marker in (
            "api key not valid",
            "api key invalid",
            "invalid api key",
            "api_key_invalid",
            "unauthenticated",
            "permission_denied",
        )
    ):
        return "auth"
    if status == 429 or any(
        marker in message for marker in ("resource_exhausted", "rate limit", "quota")
    ):
        return "rate_limit"
    if status == 404 or "model not found" in message or "not_found" in message:
        return "not_found"
    if status is not None and status >= 500:
        return "server"
    if isinstance(error, TimeoutError) or "timeout" in message or "timed out" in message:
        return "timeout"
    return "unavailable"


def _retry_after_seconds(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value:
            try:
                return max(1.0, float(value))
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(str(value))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    return max(
                        1.0,
                        (retry_at - datetime.now(timezone.utc)).total_seconds(),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
    match = re.search(r"retry[- ]after[:= ]+(\d+(?:\.\d+)?)", str(error), re.I)
    return float(match.group(1)) if match else None


def _notice_text(reason: str, tier: str) -> str:
    if reason == "task" and tier == "fast":
        return "Using a faster model for this task."
    if reason == "task":
        return "Using a more capable model for this task."
    if reason in {"rate_limit", "unavailable"}:
        return "Switched models to keep things running smoothly."
    return "Continuing your task on another model."


def _extract_chunk_text(chunk: Any) -> str:
    if isinstance(chunk, str):
        return chunk
    try:
        text = getattr(chunk, "text", None)
        if text:
            return str(text)
    except Exception:
        pass
    candidates = getattr(chunk, "candidates", None)
    if candidates and isinstance(candidates, list):
        collected: list[str] = []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            if not content:
                continue
            parts = getattr(content, "parts", None)
            if not parts or not isinstance(parts, list):
                continue
            for part in parts:
                if getattr(part, "thought", False):
                    continue
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str) and part_text:
                    collected.append(part_text)
        if collected:
            return "".join(collected)
    return ""


def _remove_duplicate_prefix(existing: str, chunk: str) -> str:
    if not existing or not chunk:
        return chunk
    maximum = min(len(existing), len(chunk))
    for size in range(maximum, 0, -1):
        if existing.endswith(chunk[:size]):
            return chunk[size:]
    if chunk.startswith(existing):
        return chunk[len(existing):]
    return chunk


def _is_qt_main_thread() -> bool:
    try:
        from PyQt6.QtCore import QCoreApplication, QThread

        app = QCoreApplication.instance()
        return app is not None and QThread.currentThread() == app.thread()
    except ImportError:
        return False


router = ModelRouter()
