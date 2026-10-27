"""Rotating audit logging for agent tasks with recursive secret redaction."""

from __future__ import annotations

import json
import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(
    r"(key|token|secret|password|authorization|cookie)", re.IGNORECASE
)


def redact(value: Any) -> Any:
    """Return a JSON-safe value with likely secrets replaced."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return value


class AuditLogger:
    """Write structured agent events to a rotating log file."""

    def __init__(self, path: str | Path = "logs/agent.log"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(f"aura.agent.{self.path.resolve()}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = RotatingFileHandler(
                self.path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self._logger.addHandler(handler)

    def event(self, event: str, **fields: Any) -> None:
        """Record one redacted JSON event."""
        payload = {"event": event, **redact(fields)}
        self._logger.info(json.dumps(payload, ensure_ascii=True, default=str))

    def tool_started(self, name: str, arguments: dict[str, Any]) -> float:
        """Record tool start and return a monotonic timer value."""
        started = time.perf_counter()
        self.event("tool_started", tool=name, arguments=arguments)
        return started

    def tool_finished(
        self, name: str, started: float, result: Any = None, error: str = ""
    ) -> None:
        """Record tool completion and elapsed duration."""
        self.event(
            "tool_finished",
            tool=name,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            result=result,
            error=error,
        )

    def close(self) -> None:
        """Close handlers owned by this audit logger."""
        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)
