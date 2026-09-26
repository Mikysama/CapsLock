"""Composition of core and dynamically discovered model tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from ..configuration import Settings
from ..lsp import LspManager
from ..mcp import McpManager
from ..plugins import PluginRegistry
from ..tooling.permission_policy.engine import PermissionEngine
from ..tooling.permission_policy.middleware import PermissionMiddleware
from ..tooling.contracts import PlanToolVisibility, ToolDefinition
from ..tooling.executor import ToolRuntime
from ..tooling.planning import PlanningBoundaryMiddleware
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
    planning: object | None = None,
) -> ToolRuntime:
    runtime = workspace_tools(
        include_collaboration=settings.agents.enabled and not child_mode,
        include_shell=settings.shell.enabled,
        include_worktree=settings.worktree.enabled and not child_mode,
        schema_budget_tokens=settings.tools.schema_budget_tokens,
        selection_mode=settings.tools.selection_mode,
        middleware=(
            PlanningBoundaryMiddleware(),
            PermissionMiddleware(permission_engine),
        ),
    )
    if child_mode:
        # Plan control belongs to the foreground session.  Do not merely rely on
        # a child capability allow-list: child runtimes constructed without one
        # must not advertise or accept the control protocol either.
        runtime = runtime.filtered(
            set(runtime.catalog._tools)
            - {"enter_plan_mode", "get_plan", "update_plan", "submit_plan"}
        )
        runtime = runtime.combined(extra_tools)
    else:
        initial = [
            *_plan_local_reads(lsp_tools(lsp)),
            *mcp_resource_tools(mcp),
            *plugin_tools(plugins),
            *mcp_tools(mcp),
        ]

        async def dynamic_tools() -> list[ToolDefinition]:
            await mcp.initialize()
            return [
                *_plan_local_reads(lsp_tools(lsp)),
                *mcp_resource_tools(mcp),
                *plugin_tools(plugins),
                *mcp_tools(mcp),
            ]

        runtime.configure_dynamic(dynamic_tools, initial)
    if allowed_names is not None:
        runtime = runtime.filtered(allowed_names)
    runtime.discover(discoveries)
    return runtime


def _plan_local_reads(tools: Iterable[ToolDefinition]) -> list[ToolDefinition]:
    return [
        replace(
            tool,
            contract=replace(
                tool.contract, plan_visibility=PlanToolVisibility.LOCAL_READ
            ),
        )
        for tool in tools
    ]


__all__ = ["build_tool_runtime"]
