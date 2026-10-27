"""Safe configuration defaults for the generalized agent layer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def get_agent_config() -> dict[str, Any]:
    """Read bounded agent settings from config.yaml and environment overrides."""
    defaults: dict[str, Any] = {
        "general_tools_enabled": True,
        "verifier_enabled": True,
        "verifier_timeout_ms": 20_000,
        "parallelism": 2,
        "background_workers": 2,
        "plan_memory_file": "memory/agent_plans.json",
        "schedule_file": "memory/agent_schedule.json",
        "workspace_dir": "workspace",
    }
    try:
        import yaml

        root = Path(__file__).resolve().parents[2]
        data = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
        configured = data.get("agent", {}) if isinstance(data, dict) else {}
        if isinstance(configured, dict):
            defaults.update(
                {key: configured[key] for key in defaults if key in configured}
            )
    except (OSError, ValueError):
        pass
    if os.environ.get("AURA_AGENT_WORKSPACE"):
        defaults["workspace_dir"] = os.environ["AURA_AGENT_WORKSPACE"]
    defaults["parallelism"] = min(4, max(1, int(defaults["parallelism"])))
    defaults["background_workers"] = min(4, max(1, int(defaults["background_workers"])))
    defaults["verifier_timeout_ms"] = min(
        120_000, max(10_000, int(defaults["verifier_timeout_ms"]))
    )
    return defaults
