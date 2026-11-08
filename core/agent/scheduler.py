"""Restart-safe interval scheduler persistence."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


class AgentScheduler:
    """Persist recurring jobs and execute due callbacks when polled."""

    def __init__(self, path: str | Path | None = None):
        from .config import get_agent_config

        self.path = Path(path or get_agent_config()["schedule_file"])
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def _save(self, jobs: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def add(self, goal: str, interval_seconds: int) -> str:
        """Persist a recurring goal with a bounded interval."""
        interval = max(60, int(interval_seconds))
        job = {
            "id": uuid.uuid4().hex,
            "goal": goal.strip(),
            "interval_seconds": interval,
            "next_run": time.time() + interval,
        }
        with self._lock:
            jobs = self._load()
            jobs.append(job)
            self._save(jobs)
        return job["id"]

    def remove(self, job_id: str) -> bool:
        """Remove a scheduled job."""
        with self._lock:
            jobs = self._load()
            remaining = [job for job in jobs if job.get("id") != job_id]
            self._save(remaining)
        return len(remaining) != len(jobs)

    def list(self) -> list[dict[str, Any]]:
        """Return persisted jobs."""
        with self._lock:
            return self._load()

    def run_due(
        self, callback: Callable[[str], Any], now: float | None = None
    ) -> list[str]:
        """Run due goals once and advance their next-run timestamps."""
        current = time.time() if now is None else now
        due: list[str] = []
        with self._lock:
            jobs = self._load()
            for job in jobs:
                if float(job.get("next_run", 0)) <= current:
                    callback(str(job.get("goal", "")))
                    job["next_run"] = current + max(
                        60, int(job.get("interval_seconds", 60))
                    )
                    due.append(str(job.get("id", "")))
            self._save(jobs)
        return due
