"""Plugin entry point for arbitrary multi-step agent tasks."""

from __future__ import annotations

import asyncio

from core.agent.background import BackgroundTaskManager
from core.agent.clarify import ClarificationRequired
from core.agent.general_task import GeneralTaskService
from core.agent.safety import ConfirmationRequired

PLUGIN = {
    "name": "agent_task",
    "description": "Plan and execute arbitrary multi-step tasks with general browser, screen, files, research, sandbox, and launcher tools; ask one clarification when required and report evidence.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {"type": "STRING"},
            "background": {"type": "BOOLEAN"},
            "parallelism": {"type": "INTEGER"},
        },
        "required": ["goal"],
    },
}

_BACKGROUND = BackgroundTaskManager(workers=2)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Run an arbitrary task in the current worker or background manager."""
    goal = str(parameters.get("goal") or "").strip()
    if not goal:
        return "What should I accomplish?"
    notify = player.write_log if player and hasattr(player, "write_log") else print
    service = GeneralTaskService(notify)
    from core.confirm import request as request_confirmation

    def gate(title: str, detail: str, operation) -> str:
        """Route preflight approval through the shared UI and voice gate."""
        return request_confirmation("agent-task", title, detail, operation)

    coroutine = service.run(
        goal,
        parallelism=min(4, max(1, int(parameters.get("parallelism", 2)))),
        confirm_request=gate,
    )
    if bool(parameters.get("background", False)):
        future = _BACKGROUND.submit(lambda: asyncio.run(coroutine))
        return f"Task started in background: {future}"
    try:
        return asyncio.run(coroutine)
    except ClarificationRequired as exc:
        notify(f"CLARIFICATION_REQUIRED: {exc.question}")
        return f"CLARIFICATION_REQUIRED: {exc.question}"
    except ConfirmationRequired as exc:
        return f"CONFIRMATION_REQUIRED: {exc.tool_name} needs human approval before execution."
