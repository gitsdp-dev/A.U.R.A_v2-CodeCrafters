"""
core/learning/learning_scheduler.py — Background Idle-Time Maintenance Worker.

Executes low-priority maintenance (memory consolidation, expiry, cap enforcement,
and candidate evaluation) on a single daemon thread when the system is idle.
Automatically pauses during active user interactions or tasks.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

LOGGER = logging.getLogger("aura.learning.scheduler")


class LearningScheduler:
    """Schedules and coordinates background learning maintenance cycles."""

    def __init__(
        self,
        maintenance_callback: Callable[[], Any],
        interval_seconds: float = 600.0,  # 10 minutes default
    ) -> None:
        self._maintenance_callback = maintenance_callback
        self._interval = max(0.2, float(interval_seconds))
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run = 0.0

    def start(self) -> None:
        """Launch background maintenance worker."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._pause_event.clear()
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="aura-learning-scheduler",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info("LearningScheduler worker started (interval=%.1fs)", self._interval)

    def pause(self) -> None:
        """Pause maintenance during active conversation turns or heavy tasks."""
        self._pause_event.set()

    def resume(self) -> None:
        """Resume background maintenance after user activity finishes."""
        self._pause_event.clear()

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def stop(self) -> None:
        """Stop maintenance thread cleanly."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        LOGGER.info("LearningScheduler worker stopped.")

    def run_now(self) -> Any:
        """Trigger an immediate maintenance cycle synchronously."""
        try:
            return self._maintenance_callback()
        except Exception as exc:
            LOGGER.error("Manual maintenance run failed: %s", exc)
            return None

    def _worker_loop(self) -> None:
        """Background thread loop with idle checks."""
        while not self._stop_event.is_set():
            # Wait in small slices to respond promptly to stop/pause
            elapsed = 0.0
            while elapsed < self._interval and not self._stop_event.is_set():
                time.sleep(1.0)
                elapsed += 1.0

            if self._stop_event.is_set():
                break

            # If paused due to active user interaction, wait until resumed
            if self._pause_event.is_set():
                continue

            try:
                self._maintenance_callback()
                self._last_run = time.time()
            except Exception as exc:
                LOGGER.error("Error during scheduled learning maintenance: %s", exc)
