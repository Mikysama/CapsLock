"""Focused McpActionHandler implementation."""
# ruff: noqa: F401

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

import httpx

from ....domain import ActionRecord, ActionResultKind, ActionType
from ....external import (
    TAVILY_SEARCH_URL,
    extract_text,
    is_suspicious,
    validate_public_url,
)
from ....policy import PolicyError, WorkspacePolicy
from ....plugins import PluginProcessClient, PluginRegistry
from ....plugins.broker import BrokerCallbacks
from ....ports import McpClientPort, SourcePort
from ..core import ActionExecution, ActionProposal
from ..executors import McpActionExecutor, PluginActionExecutor
from .transport import _preserve_manual_approval


class McpActionHandler:
    types = frozenset({ActionType.MCP_CONNECT, ActionType.MCP_CALL})

    def __init__(
        self,
        policy: WorkspacePolicy,
        *,
        output_limit_bytes: int,
        timeout_seconds: float = 30,
        plugin_registry: PluginRegistry | None = None,
        plugin_client: PluginProcessClient | None = None,
        broker_callbacks: Callable[[ActionRecord], BrokerCallbacks] | None = None,
        mcp_client: McpClientPort,
    ) -> None:
        self.policy = policy
        self.output_limit_bytes = output_limit_bytes
        self.mcp_client = mcp_client
        self.plugin_registry = plugin_registry
        self.plugin_client = plugin_client or PluginProcessClient(
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )
        self.plugin_executor = PluginActionExecutor(
            plugin_registry,
            self.plugin_client,
            output_limit_bytes=output_limit_bytes,
            policy=policy,
            broker_callbacks=broker_callbacks,
        )
        self.mcp_executor = McpActionExecutor(
            mcp_client,
            output_limit_bytes=output_limit_bytes,
        )

    async def propose(
        self, action_type: ActionType, payload: dict[str, Any]
    ) -> ActionProposal:
        plugin_name = payload.get("plugin")
        if plugin_name is not None:
            if action_type is not ActionType.MCP_CALL or not isinstance(
                plugin_name, str
            ):
                raise ValueError("plugin calls must use a plugin name")
            if self.plugin_registry is None:
                raise ValueError("plugin support is unavailable")
            entry = await asyncio.to_thread(self.plugin_registry.get, plugin_name)
            if not entry.manifest.capabilities.contains(entry.granted_capabilities):
                raise PolicyError("plugin workspace permission grant is incomplete")
            tool, arguments = payload.get("tool"), payload.get("arguments")
            if not isinstance(tool, str) or not isinstance(arguments, dict):
                raise ValueError("tool and arguments must be provided")
            if tool not in {item.name for item in entry.manifest.tools}:
                raise PolicyError(f"plugin tool is not declared: {plugin_name}.{tool}")
            request = {
                "plugin": plugin_name,
                "tool": tool,
                "arguments": arguments,
                "digest": entry.manifest.digest,
                "permissions": sorted(
                    item.value for item in entry.manifest.permissions
                ),
                "capabilities": entry.granted_capabilities.as_dict(),
                "trusted_native": entry.trusted_native,
                "force_manual_approval": bool(
                    entry.trusted_native or payload.get("force_manual_approval")
                ),
            }
            return ActionProposal(
                f"Call plugin {plugin_name}.{tool}",
                request,
            )
        server_name = payload.get("server")
        if not isinstance(server_name, str):
            raise ValueError("server must be a string")
        server = self.mcp_client.server(server_name)
        if action_type is ActionType.MCP_CONNECT:
            raise ValueError("new MCP connect Actions are no longer supported")
        tool, arguments = payload.get("tool"), payload.get("arguments")
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            raise ValueError("tool and arguments must be provided")
        if tool not in server.allowed_tools:
            raise PolicyError(
                f"MCP tool is not allowed for server {server.name}: {tool}"
            )
        request = {"server": server.name, "tool": tool, "arguments": arguments}
        _preserve_manual_approval(payload, request)
        return ActionProposal(
            f"Call MCP {server.name}.{tool}",
            request,
        )

    async def execute(self, action: ActionRecord) -> ActionExecution:
        plugin_name = action.request.get("plugin")
        if plugin_name is not None:
            result = await self.plugin_executor.execute(action)
        else:
            result = await self.mcp_executor.execute(action)
        return ActionExecution(result, ActionResultKind.SUCCESS)

    async def revalidate(self, action: ActionRecord) -> ActionProposal:
        if action.type is ActionType.MCP_CONNECT:
            server = self.mcp_client.server(str(action.request["server"]))
            request = {"server": server.name}
            _preserve_manual_approval(action.request, request)
            return ActionProposal(
                f"Refresh historical MCP connection {server.name}", request
            )
        return await self.propose(action.type, dict(action.request))

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("MCP actions cannot be reversed")
