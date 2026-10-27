"""Single-question clarification contracts for ambiguous plans."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .models import AgentPlan
from .registry import AgentToolRegistry


class ClarificationRequired(RuntimeError):
    """Raised when execution would require guessing a missing parameter."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


@dataclass(frozen=True, slots=True)
class Clarifier:
    """Produce at most one concise question for a plan."""

    ask: Callable[[str], str | Awaitable[str]]

    def question_for(self, plan: AgentPlan, registry: AgentToolRegistry) -> str:
        """Return one question when confidence or required arguments are missing."""
        confidence = plan.raw.get("confidence", 1.0)
        try:
            low_confidence = float(confidence) < 0.65
        except (TypeError, ValueError):
            low_confidence = True
        for step in plan.steps:
            spec = registry.get(step.tool)
            if spec is None:
                return f"Which available tool should I use for '{step.tool}'?"
            required = spec.parameters.get("required", [])
            missing = [key for key in required if key not in step.arguments]
            if missing:
                return f"What should I use for {missing[0]}?"
        return (
            "Can you confirm the intended outcome for this task?"
            if low_confidence
            else ""
        )
