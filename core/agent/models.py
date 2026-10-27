"""Typed data contracts used by the Phase 1 agent engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ToolRisk(StrEnum):
    """Risk categories used by the confirmation policy."""

    SAFE = "safe"
    CONFIRM = "confirm"
    DANGEROUS = "dangerous"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Description and execution contract for one agent tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    risk: ToolRisk = ToolRisk.SAFE
    timeout: float = 30.0
    run: Any = None
    source: str = ""


@dataclass(slots=True)
class PlanStep:
    """One executable step in an agent plan."""

    id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    expected_output: str = ""
    success_check: str = "output_non_empty"
    retries: int = 0


@dataclass(slots=True)
class AgentPlan:
    """Validated plan returned by a planner."""

    goal: str
    steps: list[PlanStep]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: str | dict[str, Any]) -> AgentPlan:
        """Parse and validate a planner JSON response."""
        import json
        import re

        if isinstance(payload, str):
            text = payload.strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
            data = json.loads(text)
        else:
            data = payload
        if not isinstance(data, dict):
            raise TypeError("planner response must be a JSON object")
        goal = str(data.get("goal") or "").strip()
        raw_steps = data.get("steps")
        if not goal or not isinstance(raw_steps, list):
            raise ValueError("planner response requires goal and steps")
        steps: list[PlanStep] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_steps, 1):
            if not isinstance(item, dict):
                raise TypeError(f"step {index} must be an object")
            step_id = str(item.get("id") or f"step_{index}").strip()
            tool = str(item.get("tool") or "").strip()
            arguments = item.get("arguments", {})
            dependencies = item.get("depends_on", [])
            if not step_id or step_id in seen or not tool:
                raise ValueError(f"step {index} has an invalid id or tool")
            if not isinstance(arguments, dict) or not isinstance(dependencies, list):
                raise TypeError(
                    f"step {step_id} has invalid arguments or dependencies"
                )
            seen.add(step_id)
            steps.append(
                PlanStep(
                    id=step_id,
                    tool=tool,
                    arguments=arguments,
                    depends_on=[str(value) for value in dependencies],
                    expected_output=str(item.get("expected_output") or ""),
                    success_check=str(item.get("success_check") or "output_non_empty"),
                    retries=max(0, int(item.get("retries", 0))),
                )
            )
        unknown = {dep for step in steps for dep in step.depends_on} - seen
        if unknown:
            raise ValueError(f"plan references unknown dependencies: {sorted(unknown)}")
        return cls(goal=goal, steps=steps, raw=data)
