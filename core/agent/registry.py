"""Async, typed adapters for the existing action and plugin registries."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from .models import ToolRisk, ToolSpec


def _risk_for(name: str, description: str) -> ToolRisk:
    """Infer a conservative default risk for legacy tools."""
    text = f"{name} {description}".casefold()
    dangerous = ("delete", "shutdown", "restart", "install", "send message", "forget")
    confirm = ("write", "save", "move", "rename", "change", "set ", "launch", "open")
    if any(marker in text for marker in dangerous):
        return ToolRisk.DANGEROUS
    if any(marker in text for marker in confirm):
        return ToolRisk.CONFIRM
    return ToolRisk.SAFE


class AgentToolRegistry:
    """Registry that presents legacy tools through an async execution API."""

    def __init__(self, tools: dict[str, ToolSpec]):
        self._tools = dict(tools)

    @classmethod
    def from_existing(
        cls,
        action_registry=None,
        plugin_registry=None,
        context: dict[str, Any] | None = None,
    ) -> AgentToolRegistry:
        """Wrap existing action/plugin declarations without modifying them."""
        tools: dict[str, ToolSpec] = {}
        context = dict(context or {})
        for registry, source in (
            (action_registry, "action"),
            (plugin_registry, "plugin"),
        ):
            if registry is None:
                continue
            for declaration in registry.get_tool_declarations():
                name = str(declaration.get("name") or "")
                if not name or name in tools:
                    continue
                description = str(declaration.get("description") or "")
                if source == "action":

                    async def run(
                        args, _name=name, _registry=registry, _context=context
                    ):
                        return await asyncio.to_thread(
                            _registry.run, _name, args, _context
                        )
                else:

                    async def run(
                        args, _name=name, _registry=registry, _context=context
                    ):
                        return await asyncio.to_thread(
                            _registry.run,
                            _name,
                            args,
                            player=_context.get("player"),
                            session_memory=_context.get("session_memory"),
                        )

                tools[name] = ToolSpec(
                    name=name,
                    description=description,
                    parameters=dict(declaration.get("parameters") or {}),
                    risk=_risk_for(name, description),
                    run=run,
                    source=source,
                )
        return cls(tools)

    def get(self, name: str) -> ToolSpec | None:
        """Return a tool specification by name."""
        return self._tools.get(name)

    def names(self) -> set[str]:
        """Return all registered tool names."""
        return set(self._tools)

    async def execute(
        self, name: str, arguments: dict[str, Any], timeout: float | None = None
    ) -> Any:
        """Execute a registered tool with a bounded timeout."""
        spec = self._tools.get(name)
        if spec is None or not callable(spec.run):
            raise KeyError(f"unknown agent tool: {name}")
        result = spec.run(arguments)
        if inspect.isawaitable(result):
            operation = result
        else:

            async def completed():
                return result

            operation = completed()
        return await asyncio.wait_for(operation, timeout=timeout or spec.timeout)
