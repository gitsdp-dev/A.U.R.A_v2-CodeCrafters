"""Screen Agent orchestration, safety gates, and bounded verification loop."""

from __future__ import annotations

import difflib
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .client import AgentDesktopClient, AgentDesktopError
from .config import get_settings
from .fastpath import FastIntent, match_fast_path
from .llm import (
    STATIC_SYSTEM_PROMPT,
    GeminiBackend,
    LocalOllamaBackend,
    ScreenLLM,
    ScreenLLMError,
    validate_action,
)
from .safety import app_allowed, app_context_allowed, confirmation_reason
from .tree import Element, Observation, compress_snapshot

_RATE_LIMIT = 20
_RATE_WINDOW = 60.0
_RATE_LOCK = threading.Lock()
_RECENT_RUNS: deque[float] = deque()
_ACTIVE_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_ACTIVE_EVENT: threading.Event | None = None


class ScreenAgentError(RuntimeError):
    """A user-safe Screen Agent failure."""


def cloud_allowed(settings: dict[str, Any]) -> bool:
    """Return whether the user explicitly opted in to sending screen text to Gemini."""
    return bool(settings.get("cloud_opt_in"))


def request_stop() -> None:
    """Cancel the active Screen Agent operation, including a running CLI process."""
    with _ACTIVE_LOCK:
        active = _ACTIVE_EVENT
    if active is not None:
        active.set()


def _reserve_rate_limit() -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        while _RECENT_RUNS and now - _RECENT_RUNS[0] >= _RATE_WINDOW:
            _RECENT_RUNS.popleft()
        if len(_RECENT_RUNS) >= _RATE_LIMIT:
            return False
        _RECENT_RUNS.append(now)
        return True


class ScreenAgent:
    """Run one user goal with deterministic routing and a constrained model fallback."""

    def __init__(
        self,
        *,
        settings: dict[str, Any] | None = None,
        client: AgentDesktopClient | None = None,
        llm: ScreenLLM | None = None,
        local_backend_factory: Callable[..., ScreenLLM] | None = None,
        gemini_backend_factory: Callable[..., ScreenLLM] | None = None,
        confirm: Callable[..., str] | None = None,
        status: Callable[[str], None] | None = None,
    ):
        self.settings = get_settings()
        if settings:
            self.settings.update(settings)
        self._event = threading.Event()
        self.client = client or AgentDesktopClient(
            self._event, headed=bool(self.settings["headed_input"])
        )
        self._llm_override = llm
        self._local_backend_factory = local_backend_factory or LocalOllamaBackend
        self._gemini_backend_factory = gemini_backend_factory or GeminiBackend
        self._confirm = confirm
        self._status_callback = status
        self._scope_app = ""
        self._scope_window_id = ""

    def _status(self, message: str) -> None:
        """Report a redacted action summary, never an accessibility snapshot."""
        safe_message = " ".join(str(message).split())[:180]
        if self._status_callback:
            self._status_callback(safe_message)

    def _scope_args(self) -> list[str]:
        if self._scope_window_id:
            return ["--window-id", self._scope_window_id]
        if self._scope_app:
            return ["--app", self._scope_app]
        return []

    @staticmethod
    def _snapshot_app(envelope: dict[str, Any]) -> str:
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            return ""
        app = data.get("app")
        if isinstance(app, str) and app.strip():
            return app.strip()
        window = data.get("window")
        if isinstance(window, dict):
            return str(window.get("app") or window.get("process") or window.get("title") or "").strip()
        return str(window or "").strip()

    @staticmethod
    def _snapshot_window_id(envelope: dict[str, Any]) -> str:
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            return ""
        window = data.get("window")
        if isinstance(window, dict) and window.get("id") is not None:
            return str(window["id"])
        if data.get("window_id") is not None:
            return str(data["window_id"])
        return ""

    @staticmethod
    def _snapshot_text(envelope: dict[str, Any]) -> str:
        data = envelope.get("data") if isinstance(envelope, dict) else None
        tree = data.get("tree") if isinstance(data, dict) else None
        if not isinstance(tree, dict):
            return ""
        values = []
        stack = [tree]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            for key in ("name", "value"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(children)
        return " ".join(values)

    def _allowed(self, envelope: dict[str, Any], requested_app: str = "") -> tuple[bool, str]:
        app = requested_app or self._snapshot_app(envelope)
        data = envelope.get("data") if isinstance(envelope, dict) else None
        window = data.get("window") if isinstance(data, dict) else None
        title = window.get("title", "") if isinstance(window, dict) else ""
        return app_context_allowed(
            app, str(title or ""), self.settings["allow_apps"], self.settings["deny_apps"]
        )

    def _snapshot(self, goal: str, *, app: str = "") -> tuple[dict[str, Any], Observation]:
        if self._event.is_set():
            raise AgentDesktopError("Screen Agent was stopped.", code="CANCELLED")
        if app:
            self._scope_app = app
        flags = ["--interactive-only", "--compact", *self._scope_args()]
        result = self.client.run("snapshot", *flags)
        envelope = result.envelope
        app_name = self._snapshot_app(envelope)
        if app_name:
            self._scope_app = app_name
        window_id = self._snapshot_window_id(envelope)
        if window_id:
            self._scope_window_id = window_id
        observation = compress_snapshot(
            envelope,
            goal,
            token_budget=max(self.settings["token_budget"], self.settings["gemini_token_budget"]),
            system_prompt=STATIC_SYSTEM_PROMPT,
        )
        if self._interactive_count(envelope) > 40:
            try:
                drilled = self._drill_dense_tree(goal, envelope)
            except AgentDesktopError:
                drilled = None
            if drilled is not None:
                envelope = drilled
                observation = compress_snapshot(
                    envelope,
                    goal,
                    token_budget=max(self.settings["token_budget"], self.settings["gemini_token_budget"]),
                    system_prompt=STATIC_SYSTEM_PROMPT,
                )
        return envelope, observation

    @staticmethod
    def _interactive_count(envelope: dict[str, Any]) -> int:
        data = envelope.get("data") if isinstance(envelope, dict) else None
        tree = data.get("tree") if isinstance(data, dict) else None
        count = 0
        stack = [tree]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            if node.get("ref_id") and (node.get("available_actions") or node.get("role")):
                count += 1
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(children)
        return count

    def _drill_dense_tree(self, goal: str, envelope: dict[str, Any]) -> dict[str, Any] | None:
        """Use a skeleton ref to narrow unusually dense accessibility trees."""
        skeleton_args = [
            "snapshot", "--skeleton", "--interactive-only", "--compact",
            *self._scope_args(),
        ]
        skeleton = self.client.run(*skeleton_args).envelope
        data = skeleton.get("data")
        tree = data.get("tree") if isinstance(data, dict) else None
        if not isinstance(tree, dict):
            return None
        goal_words = set(re.findall(r"[a-z0-9]+", goal.casefold()))
        candidates: list[tuple[int, str]] = []
        stack = [tree]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            ref = node.get("ref_id")
            children = node.get("children")
            name = str(node.get("name") or "")
            if ref and isinstance(children, list) and children:
                score = sum(word in name.casefold() for word in goal_words)
                candidates.append((score, str(ref)))
            if isinstance(children, list):
                stack.extend(children)
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return self.client.run(
            "snapshot", "--interactive-only", "--compact", "--root", candidates[0][1],
            *self._scope_args(),
        ).envelope

    def _observation(self, envelope: dict[str, Any], goal: str, engine: str) -> Observation:
        budget_key = "gemini_token_budget" if engine == "gemini" else "token_budget"
        return compress_snapshot(
            envelope,
            goal,
            token_budget=self.settings[budget_key],
            system_prompt=STATIC_SYSTEM_PROMPT,
        )

    def _make_backend(self, engine: str) -> ScreenLLM:
        if self._llm_override is not None:
            return self._llm_override
        if engine == "gemini":
            if not cloud_allowed(self.settings):
                raise ScreenLLMError(
                    "Gemini screen access is blocked until you opt in under Screen Agent settings."
                )
            return self._gemini_backend_factory(
                token_budget=self.settings["gemini_token_budget"],
                cancel_event=self._event,
            )
        return self._local_backend_factory(
            token_budget=self.settings["token_budget"],
            cancel_event=self._event,
        )

    def _confirmation(
        self,
        action: dict[str, Any],
        *,
        goal: str,
        app: str,
        element: Element | None,
        snapshot_text: str,
        command: Callable[[], str],
    ) -> str | None:
        reason = confirmation_reason(
            action, goal=goal, element=element, snapshot_text=snapshot_text
        )
        if not reason:
            return None
        if self._confirm is None:
            from core import confirm as confirmation_gate

            confirm = confirmation_gate.request
            pending_title = confirmation_gate.pending_title
        else:
            confirm = self._confirm
            pending_title = lambda: ""
        title = f"Screen Agent: {reason}"
        if pending_title():
            return "Another A.U.R.A. confirmation is already waiting; no screen action was taken."
        role = element.role if element else "application"
        name = element.name if element else app
        detail = f"{app} → {name} ({role}) → {action.get('a') or action.get('action')}"
        self._status(f"Awaiting confirmation: {reason}.")
        return confirm(
            f"screen-agent-{time.monotonic_ns()}",
            title,
            detail,
            command,
        )

    def _run_confirmed_cli(
        self, command: str, *args: str, app: str = ""
    ) -> str:
        self.client.run(command, *args)
        self._status(f"Completed {command}.")
        return f"{command} completed."

    def _element_for_type(self, observation: Observation) -> Element | None:
        candidates = [
            element for element in observation.elements.values()
            if element.role in {"text", "combo"} or "textbox" in element.role
        ]
        focused = [element for element in candidates if "focused" in element.states]
        return focused[0] if len(focused) == 1 else None

    @staticmethod
    def _delivery_verified(result: Any) -> bool:
        disposition = result.data.get("disposition")
        return (
            isinstance(disposition, dict)
            and disposition.get("delivery") == "delivered_verified"
        )

    def _execute_fast_action(
        self,
        command: str,
        args: list[str],
        goal: str,
        envelope: dict[str, Any],
        observation: Observation,
        *,
        action: dict[str, Any] | None = None,
    ) -> bool:
        """Execute, safely retry stale refs once, and verify an accessibility change."""
        baseline = observation.fingerprint
        try:
            result = self.client.run(command, *args)
        except AgentDesktopError as exc:
            if action is None:
                raise
            retried = self._retry_stale(exc, action, observation, goal)
            if retried is None:
                raise
            envelope, observation, action = retried
            baseline = observation.fingerprint
            command, args, _ = self._action_command(action, observation)
            result = self.client.run(command, *args)

        updated_envelope, updated = self._snapshot(
            goal, app=self._snapshot_app(envelope) or self._scope_app
        )
        allowed, reason = self._allowed(updated_envelope)
        if not allowed:
            raise AgentDesktopError(reason, code="APP_BLOCKED")
        changed = updated.fingerprint != baseline
        if not changed and not self._delivery_verified(result):
            self._status(f"{command} produced no observable accessibility change.")
            return False
        self._status(f"Verified accessibility state after {command}.")
        return True

    def _find_label(self, label: str) -> tuple[dict[str, Any], Element | None]:
        exact = self.client.run(
            "find", *self._scope_args(), "--name", label, "--exact", "--first"
        ).data
        item = exact.get("match")
        if isinstance(item, dict):
            element = self._element_from_match(item, 1)
            return exact, element
        fuzzy = self.client.run(
            "find", *self._scope_args(), "--name", label, "--limit", "20"
        ).data
        matches = fuzzy.get("matches")
        if not isinstance(matches, list):
            return fuzzy, None
        ranked: list[tuple[float, dict[str, Any]]] = []
        for candidate in matches:
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name") or "")
            ratio = difflib.SequenceMatcher(None, label.casefold(), name.casefold()).ratio()
            ranked.append((ratio, candidate))
        ranked.sort(key=lambda result: result[0], reverse=True)
        if not ranked or ranked[0][0] < 0.72:
            return fuzzy, None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
            return fuzzy, None
        return fuzzy, self._element_from_match(ranked[0][1], 1)

    @staticmethod
    def _element_from_match(item: dict[str, Any], number: int) -> Element | None:
        ref = item.get("ref_id") or item.get("ref")
        if not isinstance(ref, str) or not re.fullmatch(r"@[^:\s]+:e\d+", ref):
            return None
        return Element(
            number=number,
            role=str(item.get("role") or "node"),
            name=str(item.get("name") or ""),
            value=str(item.get("value") or ""),
            ref=ref,
            states=tuple(str(state).casefold() for state in item.get("states", []) if isinstance(state, str)),
        )

    def _fast_path(self, intent: FastIntent, goal: str) -> str | None:
        """Execute a fully-resolved deterministic intent, else request model help."""
        if intent.action in {"list_apps", "list_windows"}:
            data = self.client.run(intent.action.replace("_", "-")).data
            entries = (
                data.get("apps", [])
                if intent.action == "list_apps" and isinstance(data, dict)
                else data if intent.action == "list_windows" and isinstance(data, list)
                else []
            )
            names = [
                str(item.get("name") or item.get("title") or item)
                for item in entries if isinstance(item, (dict, str))
            ] if isinstance(entries, list) else []
            return ", ".join(names[:25]) if names else "No apps or windows were reported."

        if intent.action in {"clipboard_get", "clipboard_set"}:
            if intent.action == "clipboard_get":
                data = self.client.run("clipboard-get", "--format", "text").data
                if data.get("found") is False:
                    return "The clipboard is empty."
                return str(data.get("text") or "")
            self.client.run("clipboard-set", intent.text)
            verified = self.client.run("clipboard-get", "--format", "text").data
            if str(verified.get("text") or "") != intent.text:
                return "Clipboard text was set, but its contents could not be verified."
            return "Clipboard text updated."

        requested_app = intent.app if intent.action in {"launch", "launch_type", "focus", "close"} else ""
        if requested_app:
            allowed, reason = app_allowed(
                requested_app, self.settings["allow_apps"], self.settings["deny_apps"]
            )
            if not allowed:
                return reason

        if intent.action in {"launch", "launch_type"}:
            self.client.run("launch", intent.app)
            self._scope_app = intent.app
            self._scope_window_id = ""
            self._status("Launched the requested app.")
            if intent.action == "launch":
                return "Opened the requested app."
            for attempt in range(5):
                if self._event.wait(0.35):
                    return "Screen Agent was stopped."
                try:
                    envelope, observation = self._snapshot(goal, app=intent.app)
                except AgentDesktopError:
                    if attempt == 4:
                        raise
                    continue
                allowed, reason = self._allowed(envelope, intent.app)
                if not allowed:
                    return reason
                element = self._element_for_type(observation)
                if element is None:
                    continue
                action = {"a": "type", "n": element.number, "t": intent.text}
                snapshot_text = self._snapshot_text(envelope)
                confirm_result = self._confirmation(
                    action, goal=goal, app=intent.app, element=element,
                    snapshot_text=snapshot_text,
                    command=lambda ref=element.ref, text=intent.text, app=intent.app: self._run_confirmed_cli(
                        "type", ref, text, app=app
                    ),
                )
                if confirm_result is not None:
                    return confirm_result
                verified = self._execute_fast_action(
                    "type", [element.ref, intent.text], goal, envelope, observation,
                    action=action,
                )
                if verified:
                    return "Opened the requested app and typed the requested text."
                return "The text was sent, but no accessibility change was observed."
            return None

        if intent.action == "focus":
            self.client.run("focus-window", "--app", intent.app)
            return "Focused the requested app."
        if intent.action == "close":
            envelope, _ = self._snapshot(goal, app=intent.app)
            allowed, reason = self._allowed(envelope, intent.app)
            if not allowed:
                return reason
            command = lambda: self._run_confirmed_cli("close-app", intent.app, app=intent.app)
            confirm_result = self._confirmation(
                {"a": "close-app"}, goal=goal, app=intent.app, element=None,
                snapshot_text=self._snapshot_text(envelope), command=command,
            )
            if confirm_result is not None:
                return confirm_result
            self.client.run("close-app", intent.app)
            return "Closed the requested app."

        envelope, observation = self._snapshot(goal)
        allowed, reason = self._allowed(envelope)
        if not allowed:
            return reason
        app = self._snapshot_app(envelope)
        snapshot_text = self._snapshot_text(envelope)

        if intent.action == "click":
            _, element = self._find_label(intent.label)
            if element is None:
                return None
            action = {"a": "click", "n": element.number, "t": None}
            confirm_result = self._confirmation(
                action, goal=goal, app=app, element=element,
                snapshot_text=snapshot_text,
                command=lambda: self._run_confirmed_cli("click", element.ref, app=app),
            )
            if confirm_result is not None:
                return confirm_result
            click_observation = Observation(
                observation.text,
                {element.number: element.ref},
                {element.number: element},
                observation.fingerprint,
                observation.app,
                observation.window_id,
            )
            verified = self._execute_fast_action(
                "click", [element.ref], goal, envelope, click_observation, action=action
            )
            if verified:
                return "Clicked the requested control."
            return "The click was sent, but no accessibility change was observed."

        if intent.action == "type":
            element = self._element_for_type(observation)
            if element is None:
                return None
            action = {"a": "type", "n": element.number, "t": intent.text}
            confirm_result = self._confirmation(
                action, goal=goal, app=app, element=element,
                snapshot_text=snapshot_text,
                command=lambda: self._run_confirmed_cli("type", element.ref, intent.text, app=app),
            )
            if confirm_result is not None:
                return confirm_result
            verified = self._execute_fast_action(
                "type", [element.ref, intent.text], goal, envelope, observation,
                action=action,
            )
            if verified:
                return "Typed the requested text."
            return "The text was sent, but no accessibility change was observed."

        if intent.action == "key":
            verified = self._execute_fast_action(
                "press", [intent.key], goal, envelope, observation,
                action={"a": "key", "n": None, "t": intent.key},
            )
            return (
                f"Pressed {intent.key}."
                if verified else f"Pressed {intent.key}, but no accessibility change was observed."
            )

        if intent.action == "scroll":
            target = next(
                (element for element in observation.elements.values()
                 if "scroll" in element.role or "scrollable" in element.states),
                None,
            )
            if target is None:
                if intent.direction not in {"up", "down"}:
                    return None
                key = "PAGE_UP" if intent.direction == "up" else "PAGE_DOWN"
                verified = self._execute_fast_action(
                    "press", [key], goal, envelope, observation,
                    action={"a": "key", "n": None, "t": key},
                )
            else:
                action = {"a": "scroll", "n": target.number, "t": intent.direction}
                verified = self._execute_fast_action(
                    "scroll", [target.ref, "--direction", intent.direction],
                    goal, envelope, observation, action=action,
                )
            if verified:
                return f"Scrolled {intent.direction}."
            return "Scroll was sent, but no accessibility change was observed."

        if intent.action in {"maximize", "minimize"}:
            command = "maximize" if intent.action == "maximize" else "minimize"
            verified = self._execute_fast_action(
                command, self._scope_args(), goal, envelope, observation
            )
            return (
                f"Window {intent.action}d."
                if verified else f"Window {intent.action} was requested, but could not be verified."
            )
        if intent.action == "move":
            verified = self._execute_fast_action(
                "move-window",
                [*self._scope_args(), "--x", str(intent.x), "--y", str(intent.y)],
                goal, envelope, observation,
            )
            return "Moved the active window." if verified else "Window movement could not be verified."
        if intent.action == "resize":
            verified = self._execute_fast_action(
                "resize-window",
                [
                    *self._scope_args(),
                    "--width", str(intent.width), "--height", str(intent.height),
                ],
                goal, envelope, observation,
            )
            return "Resized the active window." if verified else "Window resizing could not be verified."
        return None

    def _action_command(
        self, action: dict[str, Any], observation: Observation
    ) -> tuple[str, list[str], Element | None]:
        kind = action["a"]
        element = observation.elements.get(action["n"]) if action["n"] is not None else None
        if kind in {"click", "type"} and element is None:
            raise ScreenLLMError("The action target is no longer in the current snapshot.")
        if kind == "click":
            return "click", [element.ref], element
        if kind == "type":
            return "type", [element.ref, action["t"]], element
        if kind == "key":
            return "press", [action["t"]], None
        if kind == "scroll":
            if element is None:
                element = next(
                    (item for item in observation.elements.values()
                     if "scroll" in item.role or "scrollable" in item.states),
                    None,
                )
            if element is None:
                raise ScreenLLMError("No scrollable element is available in this snapshot.")
            return "scroll", [element.ref, "--direction", action["t"]], element
        raise ScreenLLMError("The model selected a non-executable action.")

    def _retry_stale(
        self,
        error: AgentDesktopError,
        action: dict[str, Any],
        observation: Observation,
        goal: str,
    ) -> tuple[dict[str, Any], Observation, dict[str, Any]] | None:
        if error.code not in {"STALE_REF", "AMBIGUOUS_TARGET"} or error.retry != "safe":
            return None
        old_element = observation.elements.get(action["n"]) if action["n"] is not None else None
        if old_element is None:
            return None
        envelope, fresh = self._snapshot(goal, app=self._scope_app)
        allowed, _ = self._allowed(envelope)
        if not allowed:
            return None
        matches = [
            item for item in fresh.elements.values()
            if item.role == old_element.role and item.name.casefold() == old_element.name.casefold()
        ]
        if len(matches) != 1:
            return None
        element = matches[0]
        retried = dict(action)
        retried["n"] = element.number
        return envelope, fresh, retried

    def _run_model_loop(self, goal: str, envelope: dict[str, Any], observation: Observation) -> str:
        engine = self.settings["engine"]
        local_backend: ScreenLLM | None = None
        gemini_backend: ScreenLLM | None = None
        failed_steps = 0
        consecutive_noops = 0
        max_steps = self.settings["max_steps"]
        if engine == "gemini" and not cloud_allowed(self.settings):
            return "Screen Agent cannot send accessibility text to Gemini until cloud access is enabled in its settings."
        try:
            if self._llm_override is not None:
                local_backend = self._llm_override
            elif engine == "gemini":
                gemini_backend = self._make_backend("gemini")
            else:
                local_backend = self._make_backend("local")
        except ScreenLLMError as exc:
            failed_steps += 1
            if (
                engine == "auto"
                and self.settings["escalate_enabled"]
                and failed_steps >= self.settings["escalation_after"]
                and cloud_allowed(self.settings)
            ):
                try:
                    gemini_backend = self._make_backend("gemini")
                except ScreenLLMError as cloud_error:
                    return str(cloud_error)
            else:
                return str(exc)

        for step in range(max_steps):
            if self._event.is_set():
                return "Screen Agent was stopped."
            if engine == "auto" and (
                local_backend is None
                and gemini_backend is None
                and self.settings["escalate_enabled"]
                and failed_steps >= self.settings["escalation_after"]
                and cloud_allowed(self.settings)
            ):
                try:
                    gemini_backend = self._make_backend("gemini")
                except ScreenLLMError as exc:
                    return str(exc)
            backend = gemini_backend if gemini_backend is not None else local_backend
            active_engine = "gemini" if gemini_backend is not None else "local"
            if backend is None:
                return "Screen Agent has no permitted model backend."
            allowed, reason = self._allowed(envelope)
            if not allowed:
                return reason
            current_observation = self._observation(envelope, goal, active_engine)
            try:
                proposed = backend.next_action(goal, current_observation)
                action = validate_action(proposed, set(current_observation.refs))
            except ScreenLLMError as exc:
                failed_steps += 1
                self._status(f"Model step {step + 1} failed.")
                if (
                    engine == "auto"
                    and local_backend is not None
                    and self.settings["escalate_enabled"]
                    and failed_steps >= self.settings["escalation_after"]
                    and cloud_allowed(self.settings)
                ):
                    try:
                        gemini_backend = gemini_backend or self._make_backend("gemini")
                        envelope, observation = self._snapshot(goal, app=self._scope_app)
                        continue
                    except (ScreenLLMError, AgentDesktopError) as cloud_error:
                        return str(cloud_error)
                if (
                    engine == "auto"
                    and self.settings["escalate_enabled"]
                    and cloud_allowed(self.settings)
                    and failed_steps < self.settings["escalation_after"]
                    and step + 1 < max_steps
                ):
                    continue
                return str(exc)

            if action["a"] == "done":
                return "Screen task completed."
            if action["a"] == "fail":
                failed_steps += 1
                if (
                    engine == "auto"
                    and local_backend is not None
                    and self.settings["escalate_enabled"]
                    and failed_steps >= self.settings["escalation_after"]
                    and cloud_allowed(self.settings)
                ):
                    try:
                        gemini_backend = gemini_backend or self._make_backend("gemini")
                        envelope, observation = self._snapshot(goal, app=self._scope_app)
                        continue
                    except (ScreenLLMError, AgentDesktopError) as cloud_error:
                        return str(cloud_error)
                if (
                    engine == "auto"
                    and self.settings["escalate_enabled"]
                    and cloud_allowed(self.settings)
                    and failed_steps < self.settings["escalation_after"]
                    and step + 1 < max_steps
                ):
                    continue
                return "The model could not determine a safe next action."

            old_fingerprint = current_observation.fingerprint
            if action["a"] == "wait":
                delay = float(action["t"] or "1") if action["t"] is not None else 1.0
                if self._event.wait(delay):
                    return "Screen Agent was stopped."
                self._status(f"Waited {delay:g} second(s).")
            else:
                try:
                    command, args, element = self._action_command(action, current_observation)
                    target_app = self._snapshot_app(envelope)
                    confirm_result = self._confirmation(
                        action,
                        goal=goal,
                        app=self._snapshot_app(envelope),
                        element=element,
                        snapshot_text=self._snapshot_text(envelope),
                        command=lambda c=command, a=tuple(args), app=target_app: self._run_confirmed_cli(
                            c, *a, app=app
                        ),
                    )
                    if confirm_result is not None:
                        return confirm_result
                    self._status(f"Step {step + 1}: {command}.")
                    try:
                        self.client.run(command, *args)
                    except AgentDesktopError as exc:
                        retried = self._retry_stale(exc, action, current_observation, goal)
                        if retried is None:
                            suggestion = f" {exc.suggestion}" if exc.suggestion else ""
                            return f"{exc}{suggestion}"
                        envelope, current_observation, action = retried
                        command, args, _ = self._action_command(action, current_observation)
                        self.client.run(command, *args)
                except AgentDesktopError as exc:
                    suggestion = f" {exc.suggestion}" if exc.suggestion else ""
                    return f"{exc}{suggestion}"

            try:
                envelope, observation = self._snapshot(goal, app=self._scope_app)
            except AgentDesktopError as exc:
                suggestion = f" {exc.suggestion}" if exc.suggestion else ""
                return f"Action ran, but verification failed: {exc}{suggestion}"
            allowed, reason = self._allowed(envelope)
            if not allowed:
                return reason
            if observation.fingerprint == old_fingerprint:
                failed_steps += 1
                consecutive_noops += 1
                self._status(f"Step {step + 1} produced no accessibility change.")
                if consecutive_noops >= 2:
                    return "Screen Agent stopped after two consecutive actions with no observable change."
                if (
                    engine == "auto"
                    and local_backend is not None
                    and self.settings["escalate_enabled"]
                    and failed_steps >= self.settings["escalation_after"]
                    and cloud_allowed(self.settings)
                ):
                    try:
                        gemini_backend = gemini_backend or self._make_backend("gemini")
                    except ScreenLLMError as exc:
                        return str(exc)
            else:
                consecutive_noops = 0
            self._status(f"Verified accessibility state after step {step + 1}.")
        return f"Screen Agent stopped at its {max_steps}-step limit."

    def run(self, goal: str) -> str:
        """Run the fast path or bounded accessibility-tree model loop."""
        global _ACTIVE_EVENT
        request = " ".join(str(goal).split()).strip()
        if not self.settings["enabled"]:
            return "Screen Agent is disabled in its settings."
        if not request:
            return "Please describe the screen task."
        if not _reserve_rate_limit():
            return "Screen Agent rate limit reached. Wait a minute before trying again."
        if self.settings["engine"] == "gemini" and not cloud_allowed(self.settings):
            return "Enable the explicit cloud privacy opt-in before using Gemini for screen access."
        if not self.client.available:
            self._status("agent-desktop is missing; install it with npm install -g agent-desktop.")
            return "agent-desktop is missing. Install it with `npm install -g agent-desktop`."
        if not _RUN_LOCK.acquire(blocking=False):
            return "Screen Agent is already handling a desktop task."

        with _ACTIVE_LOCK:
            _ACTIVE_EVENT = self._event
        try:
            self._status("Screen Agent started.")
            intent = match_fast_path(request)
            if intent is not None:
                try:
                    result = self._fast_path(intent, request)
                except AgentDesktopError as exc:
                    suggestion = f" {exc.suggestion}" if exc.suggestion else ""
                    return f"{exc}{suggestion}"
                if result is not None:
                    self._status("Screen Agent finished.")
                    return result
            envelope, observation = self._snapshot(request)
            allowed, reason = self._allowed(envelope)
            if not allowed:
                return reason
            return self._run_model_loop(request, envelope, observation)
        except AgentDesktopError as exc:
            suggestion = f" {exc.suggestion}" if exc.suggestion else ""
            return f"{exc}{suggestion}"
        except (ScreenLLMError, ValueError, OSError) as exc:
            return str(exc)
        finally:
            with _ACTIVE_LOCK:
                if _ACTIVE_EVENT is self._event:
                    _ACTIVE_EVENT = None
            _RUN_LOCK.release()


def run_screen_agent_action(parameters: dict[str, Any], player=None) -> str:
    """Action-registry adapter; executes on the existing worker executor."""
    goal = str(parameters.get("goal") or parameters.get("request") or "").strip()
    status_callback = getattr(player, "write_log", None)
    if callable(status_callback):
        def report(message: str) -> None:
            status_callback(f"SYS: Screen Agent — {message}")
    else:
        report = None
    return ScreenAgent(status=report).run(goal)
