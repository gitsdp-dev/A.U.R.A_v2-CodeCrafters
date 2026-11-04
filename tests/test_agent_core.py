from __future__ import annotations

import asyncio
from pathlib import Path

from core.agent.audit import AuditLogger, redact
from core.agent.engine import AgentEngine
from core.agent.models import AgentPlan, ToolRisk, ToolSpec
from core.agent.registry import AgentToolRegistry
from core.agent.state import TaskStateStore


def _engine(tmp_path: Path, tool: ToolSpec, **kwargs) -> AgentEngine:
    audit = AuditLogger(tmp_path / "agent.log")
    engine = AgentEngine(
        AgentToolRegistry({tool.name: tool}),
        audit=audit,
        state=TaskStateStore(tmp_path / "task.json"),
        **kwargs,
    )
    engine._test_audit = audit
    return engine


def test_plan_parser_and_registry_timeout_contract():
    plan = AgentPlan.from_json(
        {
            "goal": "demo",
            "steps": [{"id": "first", "tool": "demo", "success_check": "contains:ok"}],
        }
    )
    assert plan.steps[0].tool == "demo"
    assert ToolRisk.DANGEROUS.value == "dangerous"

    async def run(_arguments):
        return "ok"

    registry = AgentToolRegistry(
        {
            "demo": ToolSpec("demo", "demo", {"type": "OBJECT"}, run=run, timeout=1),
        }
    )
    assert asyncio.run(registry.execute("demo", {})) == "ok"


def test_engine_retries_and_persists_audit(tmp_path):
    calls = 0

    def run(_arguments):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        return "ok"

    tool = ToolSpec("demo", "demo", {"type": "OBJECT"}, run=run)
    engine = _engine(tmp_path, tool)
    plan = AgentPlan.from_json(
        {
            "goal": "retry",
            "steps": [{"id": "first", "tool": "demo", "retries": 1}],
        }
    )
    result = asyncio.run(engine.run(plan))
    engine._test_audit.close()
    assert result.success
    assert calls == 2
    assert not (tmp_path / "task.json").exists()
    assert "tool_started" in (tmp_path / "agent.log").read_text(encoding="utf-8")


def test_engine_repairs_failed_plan(tmp_path):
    def run(_arguments):
        return ""

    replacement = AgentPlan.from_json(
        {
            "goal": "repair",
            "steps": [{"id": "fixed", "tool": "demo", "success_check": "always"}],
        }
    )

    async def repair(_goal, _error, _context):
        return replacement

    tool = ToolSpec("demo", "demo", {"type": "OBJECT"}, run=run)
    engine = _engine(tmp_path, tool, repair=repair, max_iterations=2)
    plan = AgentPlan.from_json(
        {
            "goal": "repair",
            "steps": [{"id": "broken", "tool": "demo"}],
        }
    )
    result = asyncio.run(engine.run(plan))
    engine._test_audit.close()
    assert result.success
    assert result.repaired


def test_audit_redacts_secrets():
    value = redact({"api_key": "secret", "nested": [{"token": "hidden"}]})
    assert value == {"api_key": "[REDACTED]", "nested": [{"token": "[REDACTED]"}]}
