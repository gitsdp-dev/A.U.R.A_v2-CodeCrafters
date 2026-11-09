"""Deterministic command recognition for common desktop operations."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FastIntent:
    """A parsed, deterministic desktop operation."""

    action: str
    app: str = ""
    text: str = ""
    key: str = ""
    direction: str = ""
    label: str = ""
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None


_PREFIX = r"^(?:aura[:,]?\s*)?"
_WEB_TARGET = re.compile(
    r"^(?:https?://|www\.)|^[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:[:/]|$)",
    re.IGNORECASE,
)


def match_fast_path(text: str) -> FastIntent | None:
    """Parse common user commands without consulting an LLM."""
    command = " ".join(text.strip().split())
    if not command:
        return None

    match = re.match(
        _PREFIX + r"(?:open|launch|start)\s+(?:the\s+)?(.+?)\s+and\s+"
        r"(?:then\s+)?type\s+(.+)$",
        command, re.IGNORECASE,
    )
    if match:
        return FastIntent("launch_type", app=match.group(1).strip(), text=match.group(2))

    match = re.match(
        _PREFIX + r"(?:copy|set)\s+(?:(.+?)\s+to\s+clipboard|"
        r"(?:to\s+)?clipboard(?:\s+to)?\s+(.+))$",
        command, re.IGNORECASE,
    )
    if match:
        return FastIntent("clipboard_set", text=match.group(1) or match.group(2))
    match = re.match(_PREFIX + r"(?:read|get|what(?:'s| is) on)\s+(?:the\s+)?clipboard$", command, re.IGNORECASE)
    if match:
        return FastIntent("clipboard_get")

    match = re.match(_PREFIX + r"(?:open|launch|start)\s+(?:the\s+)?(.+)$", command, re.IGNORECASE)
    if match and " and " not in match.group(1).casefold():
        return FastIntent("launch", app=match.group(1).strip())

    match = re.match(_PREFIX + r"focus\s+(?:(?:the\s+)?(?:window|app)\s+)?(.+)$", command, re.IGNORECASE)
    if match:
        return FastIntent("focus", app=match.group(1).strip())
    match = re.match(_PREFIX + r"close\s+(?:(?:the\s+)?(?:window|app)\s+)?(.+)$", command, re.IGNORECASE)
    if match:
        return FastIntent("close", app=match.group(1).strip())

    match = re.match(
        _PREFIX + r"click\s+(?:on\s+)?(.+?)(?:\s+in\s+(?:the\s+)?(?:current|active)\s+window)?$",
        command, re.IGNORECASE,
    )
    if match:
        return FastIntent("click", label=match.group(1).strip())

    match = re.match(_PREFIX + r"type\s+(.+)$", command, re.IGNORECASE)
    if match:
        return FastIntent("type", text=match.group(1))

    match = re.match(_PREFIX + r"(?:press|hit)\s+(?:the\s+)?(.+)$", command, re.IGNORECASE)
    if match:
        return FastIntent("key", key=match.group(1).strip())

    match = re.match(_PREFIX + r"scroll\s+(up|down|left|right)(?:\s+(.+))?$", command, re.IGNORECASE)
    if match:
        return FastIntent("scroll", direction=match.group(1).lower(), label=(match.group(2) or "").strip())

    match = re.match(_PREFIX + r"(?:maximize|maximise)\s+(?:the\s+)?(?:current\s+)?window$", command, re.IGNORECASE)
    if match:
        return FastIntent("maximize")
    match = re.match(
        _PREFIX + r"(?:minimize|minimise)\s+(?:the\s+)?(?:current\s+)?window$",
        command, re.IGNORECASE,
    )
    if match:
        return FastIntent("minimize")

    match = re.match(_PREFIX + r"move\s+(?:the\s+)?window\s+to\s+(-?\d+)\s*[, ]\s*(-?\d+)$", command, re.IGNORECASE)
    if match:
        return FastIntent("move", x=int(match.group(1)), y=int(match.group(2)))
    match = re.match(_PREFIX + r"resize\s+(?:the\s+)?window\s+to\s+(\d+)\s*(?:by|x|×)\s*(\d+)$", command, re.IGNORECASE)
    if match:
        return FastIntent("resize", width=int(match.group(1)), height=int(match.group(2)))

    if re.fullmatch(_PREFIX + r"(?:list|show)\s+(?:open\s+)?apps", command, re.IGNORECASE):
        return FastIntent("list_apps")
    if re.fullmatch(_PREFIX + r"(?:list|show)\s+(?:open\s+)?windows", command, re.IGNORECASE):
        return FastIntent("list_windows")
    return None


def is_screen_agent_request(text: str) -> bool:
    """Return whether a command is an unambiguous desktop-control fast path.

    Standalone app launches remain on A.U.R.A.'s existing ``open_app`` route,
    which also handles websites and the project's platform-specific app aliases.
    """
    intent = match_fast_path(text)
    if intent is None or intent.action == "launch":
        return False
    return not (
        intent.action == "launch_type" and _WEB_TARGET.search(intent.app.strip())
    )
