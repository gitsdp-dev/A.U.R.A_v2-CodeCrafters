"""Declarative workflow execution with progress and bounded failure handling."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class WorkflowContext:
    """Mutable data shared by workflow steps."""

    inputs: dict[str, Any] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """One named operation in a workflow definition."""

    name: str
    run: Callable[[WorkflowContext], Any]
    verify: Callable[[Any, WorkflowContext], bool] | None = None


class WorkflowError(RuntimeError):
    """Raised when a workflow step fails or cannot be verified."""


class WorkflowRunner:
    """Run declarative steps sequentially and report progress."""

    def __init__(self, notify: Callable[[str], None] | None = None):
        self.notify = notify or (lambda _message: None)

    async def run(self, steps: list[WorkflowStep], context: WorkflowContext | None = None) -> WorkflowContext:
        """Execute every step and stop at the first failed verification."""
        context = context or WorkflowContext()
        total = len(steps)
        for index, step in enumerate(steps, 1):
            self.notify(f"WORKFLOW {index}/{total}: {step.name}")
            result = step.run(context)
            if inspect.isawaitable(result):
                result = await result
            if step.verify is not None and not step.verify(result, context):
                raise WorkflowError(f"workflow verification failed: {step.name}")
            context.values[step.name] = result
            self.notify(f"WORKFLOW {index}/{total}: {step.name} complete")
        return context


def run_sync(coroutine: Awaitable[Any]) -> Any:
    """Run a workflow coroutine from a synchronous plugin worker."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise RuntimeError("workflow execution must run outside the Qt event loop")