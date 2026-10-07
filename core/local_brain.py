"""Conversation orchestration for the existing Ollama/LM Studio client."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Awaitable, Callable

from core.llm_client import call_llm_stream, gemini_tools_to_openai

_ACTION_INTENT = re.compile(
    r"\b("
    r"open|launch|close|create|delete|remove|read|list|save|"
    r"search|look up|find|check the weather|weather|"
    r"send|remind me|set a reminder|set a timer|"
    r"turn on|turn off|volume|brightness|"
    r"what time|tell me the time|today's date|system information|system status"
    r")\b",
    re.IGNORECASE,
)


def _request_needs_tools(text: str) -> bool:
    """Avoid sending large tool schemas for ordinary conversation."""
    return bool(_ACTION_INTENT.search(text))


def _exception_text(error: str | BaseException) -> str:
    """Include wrapped causes so ExceptionGroup failures can trigger fallback."""
    if isinstance(error, str):
        return error

    parts = []
    seen: set[int] = set()

    def visit(current: BaseException) -> None:
        if id(current) in seen:
            return
        seen.add(id(current))
        parts.append(str(current))
        nested = getattr(current, "exceptions", ())
        for child in nested:
            if isinstance(child, BaseException):
                visit(child)
        for related in (current.__cause__, current.__context__):
            if related is not None:
                visit(related)

    visit(error)
    return " ".join(parts)


def should_fallback_to_local(mode: str, error: str | BaseException) -> bool:
    """Decide whether a Gemini connection failure should enter fallback mode."""
    if mode != "fallback":
        return False
    message = _exception_text(error).lower()
    markers = (
        "quota", "resource_exhausted", "rate limit", "api key",
        "1007", "timeout", "timed out", "connection", "disconnected",
        "network", "socket", "getaddrinfo", "unauthorized",
        "unauthenticated", "permission_denied", "401", "403", "429",
    )
    return any(marker in message for marker in markers)


class LocalBrain:
    """Keep a bounded local conversation and stream sentence-sized replies."""

    def __init__(
        self,
        system_prompt: str,
        declarations: list[dict],
        execute_tool: Callable[[str, dict], Awaitable[str]],
        on_sentence: Callable[[str], Awaitable[None]],
        notify: Callable[[str], None],
        max_turns: int = 4,
        on_timing: Callable[[str], None] | None = None,
    ):
        self._system_prompt = system_prompt
        self._tools = gemini_tools_to_openai(declarations)
        self._schemas = {
            tool["function"]["name"]: tool["function"]["parameters"]
            for tool in self._tools
        }
        self._execute_tool = execute_tool
        self._on_sentence = on_sentence
        self._notify = notify
        self._on_timing = on_timing or notify
        self._max_turns = max(2, max_turns)
        self._history: list[dict] = []
        self._reported_tool_fallback = False

    async def ask(self, text: str) -> str:
        """Run a user turn; tools remain routed through AuraLive's guarded path."""
        text = str(text or "").strip()
        if not text:
            return ""
        self._history.append({"role": "user", "content": text})
        full_reply = ""
        request_started = time.perf_counter()
        first_sentence_reported = False
        for _ in range(3):
            messages = [
                {"role": "system", "content": self._system_prompt},
                *self._history[-self._max_turns * 2:],
            ]
            use_tools = self._tools if _request_needs_tools(text) else []
            try:
                events = call_llm_stream(
                    messages, tools=use_tools, max_tokens=96
                )
                done = {}
                while True:
                    event = await asyncio.to_thread(next, events, None)
                    if event is None:
                        break
                    if event.get("type") == "sentence":
                        sentence = str(event.get("text", "")).strip()
                        if sentence:
                            if not first_sentence_reported:
                                elapsed = time.perf_counter() - request_started
                                self._on_timing(
                                    f"SYS: Local LLM first sentence after {elapsed:.2f}s."
                                )
                                first_sentence_reported = True
                            full_reply += sentence + " "
                            await self._on_sentence(sentence)
                    elif event.get("type") == "done":
                        done = event
                tool_calls = done.get("tool_calls") or []
                if not tool_calls:
                    break
                normalized_calls = []
                for index, call in enumerate(tool_calls):
                    function = call.get("function", call) if isinstance(call, dict) else {}
                    name = function.get("name", "")
                    args = function.get("arguments", {})
                    if isinstance(args, str):
                        import json
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    if name not in self._schemas:
                        result = f"Rejected unknown local tool: {name}"
                    elif not _validate_arguments(args, self._schemas[name]):
                        result = f"Rejected invalid arguments for local tool: {name}"
                    elif name == "screen_process":
                        result = "Vision is unavailable in local mode unless a vision-capable model is configured."
                    else:
                        result = await self._execute_tool(str(name), args)
                    normalized_calls.append({
                        "id": str(call.get("id") or f"local-tool-{index}"),
                        "type": "function",
                        "function": {"name": str(name), "arguments": args},
                    })
                    self._history.append({
                        "role": "assistant",
                        "content": done.get("content") or "",
                        "tool_calls": normalized_calls[-1:],
                    })
                    self._history.append({
                        "role": "tool",
                        "tool_call_id": normalized_calls[-1]["id"],
                        "content": str(result),
                    })
            except Exception as exc:
                message = str(exc).lower()
                if use_tools and not self._reported_tool_fallback and any(
                    marker in message for marker in ("tool", "function", "unsupported", "not support")
                ):
                    self._reported_tool_fallback = True
                    self._notify("SYS: This model does not support tool calls; retrying without tools.")
                    self._tools = []
                    continue
                raise
        reply = full_reply.strip()
        if reply:
            self._history.append({"role": "assistant", "content": reply})
        self._trim_history()
        return reply



    def _trim_history(self) -> None:
        max_messages = self._max_turns * 2
        if len(self._history) > max_messages:
            self._history = self._history[-max_messages:]


def _validate_arguments(value: dict, schema: dict) -> bool:
    """Check JSON-schema required fields and primitive types before dispatch."""
    if not isinstance(value, dict) or not isinstance(schema, dict):
        return False
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    if not isinstance(required, list) or not isinstance(properties, dict):
        return False
    if any(key not in value for key in required):
        return False
    for key, item in value.items():
        field = properties.get(key)
        if field is None:
            continue
        kind = field.get("type") if isinstance(field, dict) else None
        valid = {
            "string": lambda x: isinstance(x, str),
            "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
            "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
            "boolean": lambda x: isinstance(x, bool),
            "object": lambda x: isinstance(x, dict),
            "array": lambda x: isinstance(x, list),
            "null": lambda x: x is None,
        }.get(kind)
        if valid and not valid(item):
            return False
        if isinstance(field, dict) and "enum" in field and item not in field["enum"]:
            return False
        if (isinstance(item, dict) and isinstance(field, dict)
                and not _validate_arguments(item, field)):
            return False
        if (isinstance(item, list) and isinstance(field, dict)
                and isinstance(field.get("items"), dict)
                and not all(_value_matches(element, field["items"]) for element in item)):
            return False
    return True


def _value_matches(value, schema: dict) -> bool:
    kind = schema.get("type")
    valid = {
        "string": lambda x: isinstance(x, str),
        "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
        "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
        "boolean": lambda x: isinstance(x, bool),
        "object": lambda x: isinstance(x, dict) and _validate_arguments(x, schema),
        "array": lambda x: isinstance(x, list),
        "null": lambda x: x is None,
    }.get(kind)
    if valid and not valid(value):
        return False
    return "enum" not in schema or value in schema["enum"]
