"""Parsing helpers for ordered, comma-separated user protocols."""

from __future__ import annotations

import re

_COMMAND_PREFIX = re.compile(
    r"^(?:(?:please|can you|could you)\s+)?"
    r"(?:start|run|execute|launch|begin|activate)\s+",
    re.IGNORECASE,
)


def split_protocol_tasks(description: str) -> list[str]:
    """Return non-empty protocol tasks in their original order.

    Commas are the protocol separator used by the Memory Manager. Whitespace
    around each task is ignored, while the task text itself is not rewritten.
    """
    return [task.strip() for task in description.split(",") if task.strip()]


def normalize_protocol_command(utterance: str) -> str:
    """Normalize a spoken protocol command to its likely memory title."""
    text = re.sub(r"[^\w\s-]", " ", (utterance or "").casefold())
    text = " ".join(text.split()).strip()
    text = _COMMAND_PREFIX.sub("", text)
    text = re.sub(r"^(?:the|my)\s+", "", text)
    return text.strip()


def protocol_task_count(description: str) -> int:
    """Return the number of executable comma-separated tasks."""
    return len(split_protocol_tasks(description))
