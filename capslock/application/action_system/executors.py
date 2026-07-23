"""Injected execution strategies for plugin and MCP actions."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable

from ...domain import ActionRecord
from ...mcp import McpRegistry
from ...policy import PolicyError, WorkspacePolicy
from ...plugins import PluginProcessClient, PluginRegistry
from ...plugins.broker import BrokerCallbacks, HostCapabilityBroker


class PluginActionExecutor:
    def __init__(
        self,
        registry: PluginRegistry | None,
        client: PluginProcessClient,
        *,
        output_limit_bytes: int,
        policy: WorkspacePolicy,
        broker_callbacks: Callable[[ActionRecord], BrokerCallbacks] | None = None,
    ) -> None:
        self.registry = registry
        self.client = client
        self.output_limit_bytes = output_limit_bytes
        self.policy = policy
        self.broker_callbacks = broker_callbacks

    async def execute(self, action: ActionRecord) -> dict[str, object]:
        plugin_name = action.request.get("plugin")
        if not isinstance(plugin_name, str) or self.registry is None:
            raise ValueError("plugin support is unavailable")
        entry = await asyncio.to_thread(self.registry.get, plugin_name)
        if not entry.manifest.capabilities.contains(entry.granted_capabilities):
            raise PolicyError("plugin workspace permission grant is incomplete")
        if action.request.get("digest") != entry.manifest.digest:
            raise PolicyError(
                "plugin package or workspace grant changed after approval"
            )
        broker = HostCapabilityBroker(
            self.policy,
            entry.granted_capabilities,
            callbacks=(
                self.broker_callbacks(action)
                if self.broker_callbacks is not None
                else BrokerCallbacks()
            ),
        )
        response = await self.client.call(
            entry.manifest,
            str(action.request["tool"]),
            action.request["arguments"],
            trusted_native=entry.trusted_native,
            broker=broker,
        )
        response = broker.sanitize(response)
        result: dict[str, object] = {
            "plugin": plugin_name,
            "tool": action.request["tool"],
            "result": response.get("data"),
            "plugin_ok": response.get("ok"),
            "plugin_error": response.get("error"),
            "untrusted": True,
        }
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > self.output_limit_bytes:
            return {
                "text": encoded.encode()[: self.output_limit_bytes].decode(
                    "utf-8", "ignore"
                ),
                "truncated": True,
                "untrusted": True,
            }
        return result


class McpStdioExecutor:
    def __init__(
        self,
        policy: WorkspacePolicy,
        registry: McpRegistry,
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes

    async def execute(self, action: ActionRecord) -> dict[str, object]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError("MCP support requires the mcp package") from exc
        server = await asyncio.to_thread(
            self.registry.get, str(action.request["server"])
        )
        params = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env={"PATH": os.environ.get("PATH", ""), **server.env},
            cwd=str(self.policy.command_directory(server.cwd)),
        )
        async with asyncio.timeout(self.timeout_seconds):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if action.type.value == "mcp_connect":
                        response = await session.list_tools()
                        tools = [
                            item.model_dump()
                            if hasattr(item, "model_dump")
                            else str(item)
                            for item in getattr(response, "tools", [])
                        ]
                        result: dict[str, object] = {
                            "server": server.name,
                            "tools": [
                                item
                                for item in tools
                                if not isinstance(item, dict)
                                or item.get("name") in server.allowed_tools
                            ],
                        }
                    else:
                        tool = str(action.request["tool"])
                        if tool not in server.allowed_tools:
                            raise PolicyError(
                                f"MCP tool is not allowed for server {server.name}: {tool}"
                            )
                        response = await session.call_tool(
                            tool, action.request["arguments"]
                        )
                        dumped = (
                            response.model_dump()
                            if hasattr(response, "model_dump")
                            else str(response)
                        )
                        result = {"server": server.name, "tool": tool, "result": dumped}
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > self.output_limit_bytes:
            return {
                "text": encoded.encode()[: self.output_limit_bytes].decode(
                    "utf-8", "ignore"
                ),
                "truncated": True,
            }
        decoded = json.loads(encoded)
        assert isinstance(decoded, dict)
        return decoded
