"""Injected execution strategies for plugin and MCP actions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from ...domain import ActionRecord
from ...policy import PolicyError, WorkspacePolicy
from ...plugins import PluginProcessClient, PluginRegistry
from ...plugins.broker import BrokerCallbacks, HostCapabilityBroker
from ...ports import McpClientPort


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


class McpActionExecutor:
    def __init__(
        self,
        client: McpClientPort,
        *,
        output_limit_bytes: int,
    ) -> None:
        self.client = client
        self.output_limit_bytes = output_limit_bytes

    async def execute(self, action: ActionRecord) -> dict[str, object]:
        server_name = str(action.request["server"])
        if action.type.value == "mcp_connect":
            tools = await self.client.refresh(server_name)
            result: dict[str, object] = {
                "server": server_name,
                "tools": [
                    {
                        "name": item.name,
                        "description": item.description,
                        "inputSchema": item.input_schema,
                        "outputSchema": item.output_schema,
                        "annotations": item.annotations,
                    }
                    for item in tools
                ],
            }
        else:
            tool_name = str(action.request["tool"])
            response = await self.client.call(
                server_name, tool_name, action.request["arguments"]
            )
            result = {"server": server_name, "tool": tool_name, "result": response}
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
