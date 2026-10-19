"""Offline keyword intent matching for notification and interruption commands."""

from __future__ import annotations

import re

_SHOW_READ = (
    "show and read all notifications",
    "read all notifications",
    "show all notifications",
    "show my notifications",
    "read all my notifications",
    "show me all notifications",
    "read my notifications",
    "what are my notifications",
    "any new notifications",
    "read out notifications",
    "show notifications",
    "read notifications",
)


def match_notification_intent(text: str) -> str | None:
    """Return a local notification intent; never forward matching text online."""
    normalized = re.sub(r"[^a-z0-9 ]+", " ", str(text).casefold())
    normalized = " ".join(normalized.split())
    if normalized in {"stop", "stop reading", "stop notifications", "cancel readout"}:
        return "stop"
    if normalized in {"skip", "skip this", "next notification"}:
        return "skip"
    if normalized in {"clear notifications", "clear all notifications"}:
        return "clear"
    if normalized in {"enable notifications", "turn on notifications"}:
        return "enable"
    if normalized in {"disable notifications", "turn off notifications"}:
        return "disable"
    if any(phrase in normalized for phrase in _SHOW_READ):
        return "show_read"
    if "notification" not in normalized:
        return None
    if any(word in normalized for word in ("read", "speak", "say")):
        return "read"
    if any(word in normalized for word in ("show", "list", "display", "what", "any")):
        return "show"
    return None
