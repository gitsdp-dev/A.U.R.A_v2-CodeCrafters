from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from aura.agent import tools


def create_agent(provider, config: dict, confirmer: Callable[[str], bool]):
    """Build a small-step ToolCallingAgent for the selected local provider."""
    from smolagents import ToolCallingAgent

    settings = config.get("agent", {})
    workspace = Path(
        config.get("_base_dir", Path.cwd())
    ) / settings.get("allowed_workspace_dir", "workspace")
    tools.configure_tools(str(workspace), confirmer)
    scratchpad = workspace / settings.get("scratchpad_file", ".aura-task.json")
    scratchpad.parent.mkdir(parents=True, exist_ok=True)
    if scratchpad.exists():
        try:
            json.loads(scratchpad.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read agent scratchpad {scratchpad}: {exc}") from exc
    if not scratchpad.exists():
        scratchpad.write_text(
            json.dumps({"status": "ready"}, indent=2), encoding="utf-8"
        )
    available_tools = [
        tools.list_directory,
        tools.read_text_file,
        tools.write_text_file,
        tools.create_folder,
        tools.open_application,
        tools.get_system_info,
        tools.get_time_date,
    ]
    agent = ToolCallingAgent(
        tools=available_tools,
        model=provider.agent_model(),
        max_steps=int(settings.get("max_steps", 3)),
    )

    class AgentWithScratchpad:
        def __init__(self):
            self.memory = agent.memory

        def run(self, task: str, reset: bool = True):
            state = {"status": "running", "task": task}
            scratchpad.write_text(json.dumps(state, indent=2), encoding="utf-8")
            try:
                result = agent.run(task, reset=reset)
            except Exception as exc:
                state.update({"status": "failed", "error": str(exc)})
                scratchpad.write_text(json.dumps(state, indent=2), encoding="utf-8")
                raise
            state.update({"status": "completed", "result": str(result)})
            scratchpad.write_text(json.dumps(state, indent=2), encoding="utf-8")
            return result

    return AgentWithScratchpad()
