"""Composition of core and dynamically discovered model tools."""

from __future__ import annotations

from collections.abc import Iterable
from ..configuration import Settings
from ..lsp import LspManager
from ..mcp import McpManager
from ..plugins import PluginRegistry
from ..tooling.authorization import PermissionEngine, PermissionMiddleware
from ..tooling.contracts import ToolDefinition
from ..tooling.executor import ToolRuntime
from ..tooling.tools import workspace_tools
from ..tooling.tools.lsp import lsp_tools
from ..tooling.tools.mcp import mcp_resource_tools, mcp_tools
from ..tooling.tools.plugins import plugin_tools


async def build_tool_runtime(
    *,
    settings: Settings,
    child_mode: bool,
    permission_engine: PermissionEngine,
    lsp: LspManager,
    mcp: McpManager,
    plugins: PluginRegistry,
    extra_tools: Iterable[ToolDefinition] = (),
    allowed_names: set[str] | None = None,
    discoveries: Iterable[str] = (),
) -> ToolRuntime:
    runtime = workspace_tools(
        include_collaboration=not child_mode,
        include_shell=settings.shell.enabled,
        include_worktree=settings.worktree.enabled and not child_mode,
        schema_budget_tokens=settings.tools.schema_budget_tokens,
        middleware=(PermissionMiddleware(permission_engine),),
    )
    if child_mode:
        runtime = runtime.combined(extra_tools)
    else:
        initial = [
            *lsp_tools(lsp),
            *mcp_resource_tools(mcp),
            *plugin_tools(plugins),
            *mcp_tools(mcp),
        ]

        async def dynamic_tools() -> list[ToolDefinition]:
            await mcp.initialize()
            return [
                *lsp_tools(lsp),
                *mcp_resource_tools(mcp),
                *plugin_tools(plugins),
                *mcp_tools(mcp),
            ]

        runtime.configure_dynamic(dynamic_tools, initial)
    if allowed_names is not None:
        runtime = runtime.filtered(allowed_names)
    runtime.discover(discoveries)
    return runtime


__all__ = ["build_tool_runtime"]
