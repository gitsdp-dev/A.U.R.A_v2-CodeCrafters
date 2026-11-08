"""Atomic persistence for interrupted agent tasks."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class TaskStateStore:
    """Persist the latest task state as a small JSON document."""

    def __init__(self, path: str | Path = "memory/agent_task.json"):
        self.path = Path(path)

    def save(self, state: dict[str, Any]) -> None:
        """Atomically replace the saved task state."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        os.replace(temporary, self.path)

    def load(self) -> dict[str, Any] | None:
        """Load the saved task state, returning none for missing/invalid data."""
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def clear(self) -> None:
        """Remove saved state after successful completion."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
