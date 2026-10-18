"""Typed, UI-independent contracts for local operating-system controls."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ControlState:
    """A value read from the OS, plus support and availability information."""

    control_id: str
    value: Any = None
    available: bool = True
    detail: str = ""
    error: str = ""


@dataclass(frozen=True)
class ControlResult:
    """Verified result of an attempted state change."""

    success: bool
    new_state: ControlState
    error: str = ""


class SystemControl(ABC):
    """Base contract. The manager invokes all OS methods on worker threads."""

    id: str
    label: str

    @abstractmethod
    def get_state(self) -> ControlState:
        """Read the current state from the operating system."""

    @abstractmethod
    def apply_state(self, value: Any) -> None:
        """Apply a requested value or raise an informative exception."""

    def is_supported(self) -> bool:
        """Whether this control has an implementation for the current OS."""
        return False

    def set_state(self, value: Any) -> ControlResult:
        """Apply a value, then read it back before reporting success."""
        try:
            if not self.is_supported():
                raise NotImplementedError(f"{self.label} is not supported on this platform.")
            self.apply_state(value)
            verified = self.get_state()
            if not verified.available:
                return ControlResult(
                    False, verified, verified.error or f"{self.label} could not be verified."
                )
            if not self.matches(value, verified.value):
                return ControlResult(
                    False, verified, f"{self.label} did not change to the requested value."
                )
            return ControlResult(True, verified)
        except Exception as exc:
            try:
                state = self.get_state()
            except Exception:
                state = ControlState(self.id, available=False, error=str(exc))
            return ControlResult(False, state, self.human_error(exc))

    def matches(self, requested: Any, actual: Any) -> bool:
        """Compare requested and verified values; override for compound values."""
        return requested == actual

    def human_error(self, exc: Exception) -> str:
        """Convert common OS failures into an honest short message."""
        text = str(exc).strip()
        lower = text.casefold()
        if "approval was cancelled" in lower:
            return text
        if any(word in lower for word in ("access is denied", "permission", "elevation", "admin")):
            return f"{self.label} needs administrator rights or is blocked by policy."
        if isinstance(exc, NotImplementedError):
            return str(exc)
        return text or f"Could not change {self.label.lower()}."
