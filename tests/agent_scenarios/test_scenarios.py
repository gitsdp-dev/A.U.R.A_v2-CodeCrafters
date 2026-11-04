from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.agent.engine import AgentEngine
from core.agent.models import AgentPlan, ToolSpec
from core.agent.registry import AgentToolRegistry
from core.agent.scheduler import AgentScheduler
from core.agent.state import TaskStateStore


def _run_plan(tmp_path: Path, tool_name: str, action, arguments: dict | None = None):
    registry = AgentToolRegistry(
        {
            tool_name: ToolSpec(tool_name, tool_name, {"type": "OBJECT"}, run=action),
        }
    )
    plan = AgentPlan.from_json(
        {
            "goal": tool_name,
            "steps": [{"id": "step", "tool": tool_name, "arguments": arguments or {}}],
        }
    )
    state = TaskStateStore(tmp_path / "state.json")
    return asyncio.run(AgentEngine(registry, state=state).run(plan))


@pytest.mark.parametrize(
    "name,operation",
    [
        ("file_organization", lambda _args: "organized files"),
        ("research_report", lambda _args: "report with sources"),
        ("web_form_fill", lambda _args: "form fields verified"),
        ("video_trim", lambda _args: "video output verified"),
        ("note_generation", lambda _args: "markdown note saved"),
        ("sandbox_calculation", lambda _args: "calculation complete"),
        ("app_launch", lambda _args: "application opened"),
        ("document_conversion", lambda _args: "document converted"),
        ("notification_schedule", lambda _args: "schedule persisted"),
        ("parallel_collection", lambda _args: "parallel evidence collected"),
    ],
)
def test_mock_end_to_end_scenario(tmp_path: Path, name, operation):
    result = _run_plan(tmp_path, name, operation)
    assert result.success, result.error
    assert result.context.outputs["step"]


def test_parallel_scenario_uses_bounded_independent_steps(tmp_path: Path):
    registry = AgentToolRegistry(
        {
            "collect": ToolSpec(
                "collect", "collect", {"type": "OBJECT"}, run=lambda args: args["value"]
            ),
        }
    )
    plan = AgentPlan.from_json(
        {
            "goal": "parallel",
            "steps": [
                {"id": "a", "tool": "collect", "arguments": {"value": "a"}},
                {"id": "b", "tool": "collect", "arguments": {"value": "b"}},
            ],
        }
    )
    result = asyncio.run(
        AgentEngine(
            registry, state=TaskStateStore(tmp_path / "state.json"), parallelism=2
        ).run_parallel(plan)
    )
    assert result.success
    assert result.context.outputs == {"a": "a", "b": "b"}


def test_scheduler_scenario_survives_reload(tmp_path: Path):
    path = tmp_path / "schedule.json"
    first = AgentScheduler(path)
    job_id = first.add("check downloads", 60)
    second = AgentScheduler(path)
    assert second.list()[0]["id"] == job_id
    seen = []
    second.run_due(seen.append, now=second.list()[0]["next_run"])
    assert seen == ["check downloads"]
