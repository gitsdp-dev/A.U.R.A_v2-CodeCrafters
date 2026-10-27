"""Planner implementations for Phase 1 task plans."""

from __future__ import annotations

import asyncio
from typing import Protocol

from .models import AgentPlan


class Planner(Protocol):
    """Protocol implemented by remote or test planners."""

    async def plan(self, goal: str, error: str = "") -> AgentPlan:
        """Create a validated plan for a goal."""


class GeminiPlanner:
    """Use the repository's existing Gemini fallback wrapper for planning."""

    def __init__(self, timeout_ms: int = 30_000):
        self.timeout_ms = timeout_ms

    async def plan(self, goal: str, error: str = "") -> AgentPlan:
        """Ask Gemini for strict JSON and validate it before execution."""
        from core import gemini

        prompt = (
            "Return only valid JSON with this shape: "
            "{goal: string, steps: [{id, tool, arguments, depends_on, "
            "expected_output, success_check, retries}]}. "
            "Plan only with available tools. "
            f"User goal: {goal}\n"
            f"Previous error, if any: {error}\n"
        )
        response = await asyncio.to_thread(
            gemini.text, prompt, gemini.SMART, None, self.timeout_ms
        )
        if not response:
            raise RuntimeError("planner returned no response")
        return AgentPlan.from_json(response)
