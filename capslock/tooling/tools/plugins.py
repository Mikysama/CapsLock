"""Model-tool adapters for enabled local plugins."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...domain import ActionType
from ...plugins import PluginRegistry, PluginPermission
from .actions import execute_action_tool
from ..contracts import (
    ExecutionContext,
    ResolvedToolPolicy,
    ToolDefinition,
    ToolExecution,
    ToolOutcome,
    define_tool,
)


def plugin_tools(registry: PluginRegistry) -> list[ToolDefinition]:
    tools: list[ToolDefinition] = []
    names: set[str] = set()
    for entry in registry.entries():
        if not entry.enabled:
            continue
        for spec in entry.manifest.tools:
            public_name = _public_name(entry.manifest.name, spec.name)
            if public_name in names:
                raise ValueError(f"duplicate plugin tool name: {public_name}")
            names.add(public_name)
            pure_read = (
                spec.capabilities.permissions
                == frozenset({PluginPermission.WORKSPACE_READ})
                and spec.annotations.get("read_only") is True
                and spec.annotations.get("concurrency_safe") is True
            )

            async def execute(
                context: ExecutionContext,
                arguments: dict[str, Any],
                *,
                plugin_name: str = entry.manifest.name,
                tool_name: str = spec.name,
            ) -> ToolExecution:
                outcome = await execute_action_tool(
                    context,
                    ActionType.MCP_CALL,
                    {
                        "plugin": plugin_name,
                        "tool": tool_name,
                        "arguments": arguments,
                    },
                )
                if (
                    isinstance(outcome, ToolOutcome)
                    and outcome.ok
                    and isinstance(outcome.data, dict)
                ):
                    action_result = outcome.data.get("result")
                    if isinstance(action_result, dict) and "result" in action_result:
                        return replace(outcome, data=action_result["result"])
                return outcome

            tools.append(
                define_tool(
                    public_name,
                    spec.description,
                    spec.input_schema,
                    execute,
                    output_schema=spec.output_schema,
                    search_hint=spec.search_hint,
                    deferred=spec.deferred,
                    policy=ResolvedToolPolicy(
                        read_only=pure_read,
                        concurrency_safe=pure_read,
                        external_side_effects=not pure_read,
                        open_world=True,
                        required_capabilities=frozenset(
                            item.value for item in spec.capabilities.permissions
                        ),
                    ),
                )
            )
    return tools


def _public_name(plugin_name: str, tool_name: str) -> str:
    return f"plugin__{plugin_name.replace('-', '_')}__{tool_name}"
