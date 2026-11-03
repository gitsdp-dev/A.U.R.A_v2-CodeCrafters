"""Bounded background execution for long-running agent tasks."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class BackgroundTaskManager:
    """Run background jobs with bounded workers and observable completion."""

    def __init__(self, workers: int = 2):
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="aura-agent"
        )
        self._lock = threading.Lock()
        self._futures: set[Future[Any]] = set()

    def submit(
        self,
        function: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None] | None = None,
    ) -> Future[Any]:
        """Submit a job and invoke its callback after completion."""
        future = self._executor.submit(function, *args)
        with self._lock:
            self._futures.add(future)

        def finish(done: Future[Any]) -> None:
            with self._lock:
                self._futures.discard(done)
            if on_done:
                on_done(done.result())

        future.add_done_callback(finish)
        return future

    def shutdown(self) -> None:
        """Stop accepting background jobs and wait for active jobs."""
        self._executor.shutdown(wait=True, cancel_futures=True)
