"""Registry plugin for restart-safe recurring agent goals."""

from core.agent.scheduler import AgentScheduler

PLUGIN = {
    "name": "scheduler",
    "description": "Schedule, list, remove, and run recurring agent goals that persist across restarts.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING"},
            "goal": {"type": "STRING"},
            "interval_seconds": {"type": "INTEGER"},
            "job_id": {"type": "STRING"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Run one scheduler operation."""
    scheduler = AgentScheduler()
    action = str(parameters.get("action") or "").casefold()
    if action == "add":
        return f"Scheduled job {scheduler.add(str(parameters.get('goal') or ''), int(parameters.get('interval_seconds', 86400)))}"
    if action == "list":
        return str(scheduler.list())
    if action == "remove":
        return (
            "Removed."
            if scheduler.remove(str(parameters.get("job_id") or ""))
            else "Job not found."
        )
    return "Unsupported scheduler action."
