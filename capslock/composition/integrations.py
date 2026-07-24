"""Construction and rollback ownership for external integration resources."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from ..configuration import Settings
from ..layout import ProjectLayout
from ..lsp import LspManager
from ..mcp import McpManager, McpRegistry
from ..plugins import PluginProcessClient, PluginRegistry
from ..policy import WorkspacePolicy
from ..shell import SessionProcessManager
from ..tooling.authorization import PermissionEngine


@dataclass(frozen=True)
class IntegrationBundle:
    plugins: PluginRegistry
    plugin_client: PluginProcessClient
    permissions: PermissionEngine
    mcp: McpManager
    lsp: LspManager
    processes: SessionProcessManager


async def build_integrations(
    *,
    policy: WorkspacePolicy,
    settings: Settings,
    layout: ProjectLayout,
    journal: Any,
    resources: AsyncExitStack,
    child_mode: bool,
    plugin_registry: PluginRegistry | None = None,
) -> IntegrationBundle:
    plugins = plugin_registry or PluginRegistry(layout)
    plugin_client = PluginProcessClient(
        timeout_seconds=settings.mcp.mcp_timeout_seconds,
        output_limit_bytes=settings.mcp.mcp_output_bytes,
    )
    resources.push_async_callback(plugin_client.close)
    permissions = PermissionEngine(
        (
            ("user", layout.user.permissions),
            ("project", layout.project_permissions),
            ("local", layout.local_permissions),
        ),
        journal,
    )
    mcp = McpManager(
        policy,
        McpRegistry(policy, layout=layout),
        timeout_seconds=settings.mcp.mcp_timeout_seconds,
    )
    if not child_mode:
        await mcp.initialize()
    resources.push_async_callback(mcp.close)
    lsp = LspManager(policy, settings.lsp)
    resources.push_async_callback(lsp.close)
    processes = SessionProcessManager(settings.shell.output_bytes)
    resources.push_async_callback(processes.close)
    return IntegrationBundle(plugins, plugin_client, permissions, mcp, lsp, processes)
