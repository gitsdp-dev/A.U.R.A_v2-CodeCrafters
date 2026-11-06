"""Arbitrary-goal planning over the Phase 3.5 general toolset."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from core import gemini

from .clarify import ClarificationRequired, Clarifier
from .engine import AgentEngine
from .general_tools import (
    app_launcher_tool,
    browser_tool,
    files_tool,
    python_sandbox_tool,
    screen_tool,
    web_research_tool,
)
from .models import AgentPlan, ToolRisk, ToolSpec
from .plan_memory import PlanMemoryStore
from .registry import AgentToolRegistry
from .verifier import GoalVerifier


class GeneralTaskService:
    """Plan and execute arbitrary tasks using only registered general tools."""

    def __init__(self, notify: Callable[[str], None] | None = None):
        self.notify = notify or (lambda _message: None)
        self.memory = PlanMemoryStore()
        self.registry = AgentToolRegistry(
            {
                "browser_tool": ToolSpec(
                    "browser_tool",
                    "browser",
                    {"type": "OBJECT"},
                    risk=ToolRisk.CONFIRM,
                    run=lambda args: browser_tool(args),
                ),
                "screen_tool": ToolSpec(
                    "screen_tool",
                    "screen",
                    {"type": "OBJECT"},
                    risk=ToolRisk.CONFIRM,
                    run=lambda args: screen_tool(args),
                ),
                "files_tool": ToolSpec(
                    "files_tool",
                    "files",
                    {"type": "OBJECT"},
                    risk=ToolRisk.CONFIRM,
                    run=lambda args: files_tool(args),
                ),
                "web_research_tool": ToolSpec(
                    "web_research_tool",
                    "research",
                    {
                        "type": "OBJECT",
                    },
                    run=lambda args: web_research_tool(args),
                ),
                "python_sandbox_tool": ToolSpec(
                    "python_sandbox_tool",
                    "sandbox",
                    {"type": "OBJECT"},
                    run=lambda args: python_sandbox_tool(args),
                ),
                "app_launcher_tool": ToolSpec(
                    "app_launcher_tool",
                    "launcher",
                    {"type": "OBJECT"},
                    risk=ToolRisk.CONFIRM,
                    run=lambda args: app_launcher_tool(args),
                ),
            }
        )

    async def plan(self, goal: str) -> AgentPlan:
        """Ask Gemini for a plan constrained to registered tools."""
        suggestion = self.memory.suggest(goal)
        prompt = (
            "Return only valid JSON with goal, confidence, and steps. Each step needs "
            "id, tool, arguments, depends_on, expected_output, success_check, retries. "
            "Use only these tools: " + ", ".join(sorted(self.registry.names())) + ".\n"
            f"Goal: {goal}\n"
            f"Saved plan starting point: {json.dumps(suggestion or {}, default=str)[:4000]}"
        )
        response = await asyncio.to_thread(
            gemini.text, prompt, gemini.SMART, None, 45_000
        )
        if not response:
            raise RuntimeError("planner returned no response")
        return AgentPlan.from_json(response)

    async def execute_plan(self, plan: AgentPlan, parallelism: int = 2) -> str:
        """Execute a pre-approved plan with evidence verification."""
        engine = AgentEngine(
            self.registry,
            goal_verifier=GoalVerifier(),
            parallelism=parallelism,
            confirm=lambda _name, _arguments: True,
        )
        result = await engine.run_parallel(plan)
        if not result.success:
            return f"Task failed with evidence: {result.error}"
        self.memory.save(plan.goal, plan)
        return f"Task completed with evidence: {result.context.metadata.get('final_verification', result.context.outputs)}"

    async def run(
        self,
        goal: str,
        *,
        background: bool = False,
        parallelism: int = 2,
        confirm_request: Callable[[str, str, Callable[[], str]], str] | None = None,
    ) -> str:
        """Clarify, confirm, execute, verify, and remember a general task plan."""
        plan = await self.plan(goal)
        question = Clarifier(lambda value: value).question_for(plan, self.registry)
        if question:
            raise ClarificationRequired(question)
        risky = [
            step.tool for step in plan.steps
            if self.registry.get(step.tool)
            and self.registry.get(step.tool).risk != ToolRisk.SAFE
        ]
        if risky:
            if confirm_request is None:
                raise RuntimeError(f"confirmation required for: {', '.join(risky)}")
            return confirm_request(
                "Approve agent task actions",
                f"The task will use: {', '.join(risky)}\nGoal: {goal}",
                lambda: asyncio.run(self.execute_plan(plan, parallelism)),
            )
        return await self.execute_plan(plan, parallelism)
