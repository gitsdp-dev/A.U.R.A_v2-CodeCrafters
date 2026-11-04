"""Plan, execute, verify, and repair orchestration for agent tasks."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .audit import AuditLogger
from .models import AgentPlan, PlanStep
from .registry import AgentToolRegistry
from .safety import CancellationToken, ConfirmationRequired, SandboxPolicy
from .state import TaskStateStore
from .verifier import GoalVerifier


@dataclass(slots=True)
class TaskContext:
    """Shared context passed between ordered steps."""

    goal: str
    outputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskResult:
    """Outcome of an agent execution."""

    success: bool
    context: TaskContext
    error: str = ""
    repaired: bool = False


class AgentEngine:
    """Execute validated plans with bounded retries and verification."""

    def __init__(
        self,
        registry: AgentToolRegistry,
        *,
        audit: AuditLogger | None = None,
        state: TaskStateStore | None = None,
        cancellation: CancellationToken | None = None,
        sandbox: SandboxPolicy | None = None,
        confirm: Callable[[str, dict[str, Any]], bool | Awaitable[bool]] | None = None,
        repair: Callable[[str, str, TaskContext], AgentPlan | Awaitable[AgentPlan]]
        | None = None,
        goal_verifier: GoalVerifier | None = None,
        parallelism: int = 1,
        max_iterations: int = 3,
    ):
        self.registry = registry
        self.audit = audit or AuditLogger()
        self.state = state or TaskStateStore()
        self.cancellation = cancellation or CancellationToken()
        self.sandbox = sandbox or SandboxPolicy()
        self.confirm = confirm
        self.repair = repair
        self.goal_verifier = goal_verifier
        self.parallelism = max(1, parallelism)
        self.max_iterations = max(1, max_iterations)

    async def run(
        self, plan: AgentPlan, context: TaskContext | None = None
    ) -> TaskResult:
        """Run a plan until completion, repair, cancellation, or iteration limit."""
        context = context or TaskContext(goal=plan.goal)
        repaired = False
        current = plan
        for iteration in range(self.max_iterations):
            self.cancellation.raise_if_cancelled()
            self.audit.event("task_started", goal=current.goal, iteration=iteration)
            try:
                for step in current.steps:
                    self.cancellation.raise_if_cancelled()
                    missing = [
                        dep for dep in step.depends_on if dep not in context.outputs
                    ]
                    if missing:
                        raise RuntimeError(
                            f"step {step.id} missing dependencies: {missing}"
                        )
                    output = await self._run_step(step, context)
                    if not self.verify(step, output, context):
                        raise RuntimeError(f"verification failed for step {step.id}")
                    if self.goal_verifier is not None:
                        verification = await self.goal_verifier.verify(
                            current.goal, step, output
                        )
                        context.metadata.setdefault("verification", {})[step.id] = {
                            "passed": verification.passed,
                            "evidence": verification.evidence,
                            "what_is_missing": verification.what_is_missing,
                        }
                        if not verification.passed:
                            raise RuntimeError(
                                f"goal verification failed for step {step.id}: {verification.what_is_missing}"
                            )
                    context.outputs[step.id] = output
                    self.state.save(self._state_payload(current, context, step.id))
                if self.goal_verifier is not None and current.steps:
                    final_step = current.steps[-1]
                    final = await self.goal_verifier.verify(
                        current.goal, final_step, context.outputs, final=True
                    )
                    context.metadata["final_verification"] = {
                        "passed": final.passed,
                        "evidence": final.evidence,
                        "what_is_missing": final.what_is_missing,
                    }
                    if not final.passed:
                        raise RuntimeError(
                            f"final goal verification failed: {final.what_is_missing}"
                        )
                self.state.clear()
                self.audit.event("task_finished", goal=current.goal, success=True)
                return TaskResult(True, context, repaired=repaired)
            except asyncio.CancelledError:
                self.audit.event("task_cancelled", goal=current.goal)
                self.state.save(self._state_payload(current, context, "cancelled"))
                raise
            except (RuntimeError, KeyError, PermissionError, TimeoutError, OSError, ValueError, TypeError) as exc:
                error = str(exc)
                self.audit.event("task_failed", goal=current.goal, error=error)
                if self.repair is None or iteration + 1 >= self.max_iterations:
                    self.state.save(
                        self._state_payload(current, context, "failed", error)
                    )
                    return TaskResult(False, context, error=error, repaired=repaired)
                replacement = self.repair(current.goal, error, context)
                current = (
                    await replacement
                    if inspect.isawaitable(replacement)
                    else replacement
                )
                repaired = True
        return TaskResult(
            False, context, error="agent iteration limit reached", repaired=repaired
        )

    async def run_parallel(
        self, plan: AgentPlan, context: TaskContext | None = None
    ) -> TaskResult:
        """Execute independent ready steps concurrently with bounded workers."""
        if self.parallelism == 1:
            return await self.run(plan, context)
        context = context or TaskContext(goal=plan.goal)
        remaining = {step.id: step for step in plan.steps}
        try:
            while remaining:
                self.cancellation.raise_if_cancelled()
                ready = [
                    step
                    for step in remaining.values()
                    if all(dep in context.outputs for dep in step.depends_on)
                ]
                if not ready:
                    raise RuntimeError("plan dependencies cannot be satisfied")
                batch = ready[: self.parallelism]
                outputs = await asyncio.gather(
                    *(self._run_step(step, context) for step in batch)
                )
                for step, output in zip(batch, outputs):
                    if not self.verify(step, output, context):
                        raise RuntimeError(f"verification failed for step {step.id}")
                    if self.goal_verifier is not None:
                        verification = await self.goal_verifier.verify(plan.goal, step, output)
                        context.metadata.setdefault("verification", {})[step.id] = {
                            "passed": verification.passed,
                            "evidence": verification.evidence,
                            "what_is_missing": verification.what_is_missing,
                        }
                        if not verification.passed:
                            raise RuntimeError(f"goal verification failed for step {step.id}: {verification.what_is_missing}")
                    context.outputs[step.id] = output
                    remaining.pop(step.id)
            if self.goal_verifier is not None and plan.steps:
                final = await self.goal_verifier.verify(plan.goal, plan.steps[-1], context.outputs, final=True)
                context.metadata["final_verification"] = {
                    "passed": final.passed,
                    "evidence": final.evidence,
                    "what_is_missing": final.what_is_missing,
                }
                if not final.passed:
                    raise RuntimeError(f"final goal verification failed: {final.what_is_missing}")
            self.state.clear()
            return TaskResult(True, context)
        except (RuntimeError, KeyError, PermissionError, TimeoutError, OSError, ValueError, TypeError) as exc:
            self.state.save(self._state_payload(plan, context, "failed", str(exc)))
            return TaskResult(False, context, error=str(exc))

    async def _run_step(self, step: PlanStep, context: TaskContext) -> Any:
        """Run one step with bounded exponential backoff."""
        spec = self.registry.get(step.tool)
        if spec is None:
            raise KeyError(f"unknown agent tool: {step.tool}")
        if self.sandbox.requires_confirmation(spec.risk):
            if self.confirm is None:
                raise ConfirmationRequired(spec.name)
            accepted = self.confirm(spec.name, step.arguments)
            accepted = await accepted if inspect.isawaitable(accepted) else accepted
            if not accepted:
                raise PermissionError(f"confirmation denied for tool: {spec.name}")
        last_error: Exception | None = None
        attempts = max(0, step.retries) + 1
        for attempt in range(attempts):
            self.cancellation.raise_if_cancelled()
            started = self.audit.tool_started(spec.name, step.arguments)
            try:
                result = await self.registry.execute(spec.name, step.arguments)
                self.audit.tool_finished(spec.name, started, result=result)
                return result
            except asyncio.CancelledError:
                self.audit.tool_finished(spec.name, started, error="cancelled")
                raise
            except (RuntimeError, KeyError, PermissionError, TimeoutError, OSError, ValueError, TypeError) as exc:
                last_error = exc
                self.audit.tool_finished(spec.name, started, error=str(exc))
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(2.0**attempt, 8.0))
        assert last_error is not None
        raise last_error

    @staticmethod
    def verify(step: PlanStep, output: Any, context: TaskContext) -> bool:
        """Apply the built-in success checks supported by Phase 1."""
        check = step.success_check.strip()
        text = str(output or "")
        if check in {"", "output_non_empty"}:
            return bool(text.strip())
        if check.startswith("contains:"):
            return check.removeprefix("contains:").strip() in text
        if check.startswith("file_exists:"):
            from pathlib import Path

            return Path(check.removeprefix("file_exists:").strip()).is_file()
        if check == "always":
            return True
        return bool(text.strip())

    @staticmethod
    def _state_payload(
        plan: AgentPlan, context: TaskContext, status: str, error: str = ""
    ) -> dict[str, Any]:
        return {
            "goal": plan.goal,
            "status": status,
            "error": error,
            "updated_at": time.time(),
            "outputs": context.outputs,
        }
