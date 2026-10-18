"""Threaded registry and dispatch for system controls."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import RLock
from typing import Any, Callable

from .base import ControlResult, ControlState, SystemControl


class SystemControlsManager:
    """Runs every control operation in a bounded worker pool."""

    def __init__(self, controls: list[SystemControl] | None = None,
                 timeout_seconds: float = 8.0):
        self._controls: dict[str, SystemControl] = {}
        self._listeners: list[Callable[[str, ControlState], None]] = []
        self._lock = RLock()
        self._timeout = max(0.5, float(timeout_seconds))
        self._workers = ThreadPoolExecutor(max_workers=6, thread_name_prefix="aura-system")
        for control in controls or []:
            self.register(control)

    def register(self, control: SystemControl) -> None:
        """Register a unique control."""
        if control.id in self._controls:
            raise ValueError(f"System control '{control.id}' is already registered.")
        self._controls[control.id] = control

    def get(self, control_id: str) -> SystemControl | None:
        return self._controls.get(str(control_id))

    def subscribe(self, callback: Callable[[str, ControlState], None]) -> Callable[[], None]:
        """Subscribe to verified state changes. Returns an unsubscribe callback."""
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def snapshot(self) -> dict[str, ControlState]:
        """Read every state concurrently; individual read failures stay explicit."""
        futures = {
            control_id: self._workers.submit(control.get_state)
            for control_id, control in self._controls.items()
        }
        states: dict[str, ControlState] = {}
        for control_id, future in futures.items():
            try:
                states[control_id] = future.result(timeout=self._timeout)
            except TimeoutError:
                states[control_id] = ControlState(
                    control_id, available=False, error="Timed out while reading this setting."
                )
            except Exception as exc:
                states[control_id] = ControlState(
                    control_id, available=False, error=str(exc)
                )
        return states

    def set_state(self, control_id: str, value: Any) -> ControlResult:
        """Change a state off-thread and notify listeners only after read-back."""
        control = self.get(control_id)
        if control is None:
            return ControlResult(
                False,
                ControlState(str(control_id), available=False, error="Unknown system control."),
                "Unknown system control.",
            )
        future = self._workers.submit(control.set_state, value)
        timeout = max(
            self._timeout,
            float(getattr(control, "elevation_timeout_seconds", self._timeout)),
        )
        try:
            result = future.result(timeout=timeout)
        except TimeoutError:
            result = ControlResult(
                False,
                ControlState(control.id, available=False, error="Operation timed out."),
                f"{control.label} operation timed out.",
            )
        except Exception as exc:
            result = ControlResult(
                False, ControlState(control.id, available=False, error=str(exc)), str(exc)
            )
        if result.success:
            with self._lock:
                listeners = tuple(self._listeners)
            for callback in listeners:
                try:
                    callback(control.id, result.new_state)
                except Exception as exc:
                    print(f"[SystemControls] State listener failed: {exc}")
        return result

    def call(self, control_id: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a supported control-specific operation on a worker thread."""
        control = self.get(control_id)
        operation = getattr(control, method, None) if control else None
        if not callable(operation):
            raise ValueError(f"Operation '{method}' is not available for '{control_id}'.")
        future = self._workers.submit(operation, *args, **kwargs)
        try:
            return future.result(timeout=self._timeout)
        except TimeoutError as exc:
            raise TimeoutError(f"{method} operation timed out.") from exc

    def close(self) -> None:
        """Release worker resources without waiting for a stuck OS call."""
        self._workers.shutdown(wait=False, cancel_futures=True)
