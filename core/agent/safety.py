"""Cancellation, confirmation, and filesystem safety primitives."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from .models import ToolRisk


class ConfirmationRequired(RuntimeError):
    """Raised when a tool needs human approval before execution."""

    def __init__(self, tool_name: str):
        super().__init__(f"confirmation required for tool: {tool_name}")
        self.tool_name = tool_name


class CancellationToken:
    """Thread-safe cancellation token usable from sync and async code."""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation."""
        self._event.set()

    def is_cancelled(self) -> bool:
        """Return whether cancellation was requested."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise asyncio cancellation when the token is set."""
        if self.is_cancelled():
            raise asyncio.CancelledError()


class SandboxPolicy:
    """Restrict file operations to explicitly approved directories."""

    def __init__(self, approved_directories: list[str | Path] | None = None):
        self._roots = [
            Path(path).expanduser().resolve() for path in approved_directories or []
        ]

    def approve(self, path: str | Path) -> bool:
        """Return true when a path is inside an approved directory."""
        candidate = Path(path).expanduser().resolve()
        return any(
            candidate == root or root in candidate.parents for root in self._roots
        )

    def require(self, path: str | Path) -> Path:
        """Resolve a path or raise when it is outside the sandbox."""
        candidate = Path(path).expanduser().resolve()
        if not self.approve(candidate):
            raise PermissionError(f"path is outside approved directories: {candidate}")
        return candidate

    @staticmethod
    def requires_confirmation(risk: ToolRisk) -> bool:
        """Return whether a risk category requires human confirmation."""
        return risk in {ToolRisk.CONFIRM, ToolRisk.DANGEROUS}
