"""Secret-free persistence and reuse of successful agent plans."""

from __future__ import annotations

import json
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .models import AgentPlan


class PlanMemoryStore:
    """Store successful goal-pattern to plan mappings atomically."""

    def __init__(self, path: str | Path | None = None):
        from .config import get_agent_config

        self.path = Path(path or get_agent_config()["plan_memory_file"])
        self._lock = threading.Lock()

    @staticmethod
    def _clean(value: Any) -> Any:
        """Remove likely secrets and personal values from saved plans."""
        if isinstance(value, dict):
            return {
                key: "[REDACTED]"
                if re.search(
                    r"key|token|secret|password|cookie", str(key), re.IGNORECASE
                )
                else PlanMemoryStore._clean(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [PlanMemoryStore._clean(item) for item in value]
        if isinstance(value, str):
            return re.sub(
                r"(?:api[_-]?key|token|password)\s*[:=]\s*\S+",
                "[REDACTED]",
                value,
                flags=re.IGNORECASE,
            )[:500]
        return value

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def save(self, goal_pattern: str, plan: AgentPlan) -> None:
        """Save a successful plan without secrets or personal data."""
        entry = self._clean({"goal": goal_pattern.strip().lower(), "plan": plan.raw})
        with self._lock:
            entries = [
                item for item in self._load() if item.get("goal") != entry["goal"]
            ]
            entries.append(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(entries[-100:], indent=2), encoding="utf-8")
            temporary.replace(self.path)

    def suggest(self, goal: str) -> dict[str, Any] | None:
        """Return the closest saved plan when similarity is high enough."""
        target = goal.strip().lower()
        with self._lock:
            entries = self._load()
        ranked = sorted(
            entries,
            key=lambda item: SequenceMatcher(
                None, target, str(item.get("goal", ""))
            ).ratio(),
            reverse=True,
        )
        if (
            ranked
            and SequenceMatcher(None, target, str(ranked[0].get("goal", ""))).ratio()
            >= 0.72
        ):
            return ranked[0]
        return None
