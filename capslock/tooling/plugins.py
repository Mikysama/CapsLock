"""Model-tool adapters for enabled local plugins."""

from __future__ import annotations

from typing import Any

from ..domain import ActionType
from ..plugins import PluginRegistry
from .async_adapters import propose_action
from .async_core import RunContext, Tool, ToolResult


def plugin_tools(registry: PluginRegistry) -> list[Tool]:
    tools: list[Tool] = []
    names: set[str] = set()
    for entry in registry.entries():
        if not entry.enabled:
            continue
        for spec in entry.manifest.tools:
            public_name = _public_name(entry.manifest.name, spec.name)
            if public_name in names:
                raise ValueError(f"duplicate plugin tool name: {public_name}")
            names.add(public_name)

            async def execute(
                context: RunContext,
                arguments: dict[str, Any],
                *,
                plugin_name: str = entry.manifest.name,
                tool_name: str = spec.name,
            ) -> ToolResult:
                return await propose_action(
                    context,
                    ActionType.MCP_CALL,
                    {
                        "plugin": plugin_name,
                        "tool": tool_name,
                        "arguments": arguments,
                    },
                )

            tools.append(Tool(public_name, spec.description, spec.parameters, execute))
    return tools


def _public_name(plugin_name: str, tool_name: str) -> str:
    return f"plugin_{plugin_name.replace('-', '_')}_{tool_name}"
