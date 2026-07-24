"""Managed, reconnecting MCP stdio connections and dynamic tool metadata."""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from ..policy import WorkspacePolicy
from ..ports.mcp import ManagedMcpResource, ManagedMcpTool, McpServer
from .registry import McpRegistry


@dataclass
class _Connection:
    server: McpServer
    stack: AsyncExitStack
    session: Any
    tools: tuple[ManagedMcpTool, ...]
    resources: tuple[ManagedMcpResource, ...]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class McpManager:
    def __init__(
        self,
        policy: WorkspacePolicy,
        registry: McpRegistry,
        *,
        timeout_seconds: float = 30,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.timeout_seconds = timeout_seconds
        self._sessions: dict[str, _Connection] = {}
        self.errors: dict[str, str] = {}

    async def initialize(self) -> tuple[ManagedMcpTool, ...]:
        configured = await asyncio.to_thread(self.registry.servers)
        for name in tuple(self._sessions):
            if name not in configured:
                await self._disconnect(name)
        for name in sorted(configured):
            try:
                await self._connect(name)
            except Exception as exc:
                self.errors[name] = str(exc) or type(exc).__name__
        return self.tools()

    def server(self, name: str) -> McpServer:
        return self.registry.get(name)

    def tools(self) -> tuple[ManagedMcpTool, ...]:
        return tuple(
            tool
            for name in sorted(self._sessions)
            for tool in self._sessions[name].tools
        )

    def resources(self, server: str | None = None) -> tuple[ManagedMcpResource, ...]:
        return tuple(
            resource
            for name in sorted(self._sessions)
            if server is None or name == server
            for resource in self._sessions[name].resources
        )

    async def read_resource(self, server_name: str, uri: str) -> object:
        connection = self._sessions.get(server_name)
        if connection is None:
            connection = await self._connect(server_name)
        if uri not in {item.uri for item in connection.resources}:
            raise PermissionError(f"MCP resource is not allowed: {server_name}.{uri}")
        async with connection.lock:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await connection.session.read_resource(uri)
            except asyncio.CancelledError:
                raise
            except Exception:
                connection = await self._reconnect(server_name)
                async with connection.lock:
                    async with asyncio.timeout(self.timeout_seconds):
                        response = await connection.session.read_resource(uri)
        return (
            response.model_dump(by_alias=True)
            if hasattr(response, "model_dump")
            else response
        )

    async def refresh(self, server_name: str) -> tuple[ManagedMcpTool, ...]:
        await self._disconnect(server_name)
        connection = await self._connect(server_name)
        return connection.tools

    async def switch_policy(self, policy: WorkspacePolicy) -> None:
        """Reconnect servers so cwd and workspace grants follow the active workspace."""
        self.policy = policy
        self.registry.policy = policy
        await self.close()

    async def call(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> object:
        connection = self._sessions.get(server_name)
        if connection is None:
            connection = await self._connect(server_name)
        if tool_name not in {item.name for item in connection.tools}:
            raise PermissionError(f"MCP tool is not allowed: {server_name}.{tool_name}")
        async with connection.lock:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await connection.session.call_tool(tool_name, arguments)
            except asyncio.CancelledError:
                raise
            except Exception:
                connection = await self._reconnect(server_name)
                async with connection.lock:
                    async with asyncio.timeout(self.timeout_seconds):
                        response = await connection.session.call_tool(
                            tool_name, arguments
                        )
        return response.model_dump() if hasattr(response, "model_dump") else response

    async def _reconnect(self, name: str) -> _Connection:
        await self._disconnect(name)
        return await self._connect(name)

    async def _connect(self, name: str) -> _Connection:
        existing = self._sessions.get(name)
        if existing is not None:
            return existing
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError("MCP support requires the mcp package") from exc
        server = await asyncio.to_thread(self.registry.get, name)
        params = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env={"PATH": os.environ.get("PATH", ""), **server.env},
            cwd=str(self.policy.command_directory(server.cwd)),
        )
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))

            async def message_handler(message: Any) -> None:
                raw = getattr(message, "root", message)
                message_name = type(raw).__name__
                if "ToolListChanged" in message_name:
                    asyncio.create_task(self._refresh_metadata(server.name))
                if "ResourceListChanged" in message_name:
                    asyncio.create_task(self._refresh_resources(server.name))

            session = await stack.enter_async_context(
                ClientSession(read, write, message_handler=message_handler)
            )
            async with asyncio.timeout(self.timeout_seconds):
                await session.initialize()
                response = await session.list_tools()
                try:
                    listed_resources = await session.list_resources()
                except Exception:
                    listed_resources = None
            tools = self._listed_tools(server, response)
            resources = self._listed_resources(server, listed_resources)
            connection = _Connection(server, stack, session, tools, resources)
            self._sessions[name] = connection
            self.errors.pop(name, None)
            return connection
        except BaseException:
            await stack.aclose()
            raise

    @staticmethod
    def _listed_tools(server: McpServer, response: Any) -> tuple[ManagedMcpTool, ...]:
        tools: list[ManagedMcpTool] = []
        for raw in getattr(response, "tools", ()):
            dumped = raw.model_dump(by_alias=True) if hasattr(raw, "model_dump") else {}
            tool_name = str(dumped.get("name", getattr(raw, "name", "")))
            if not tool_name or tool_name not in server.allowed_tools:
                continue
            input_schema = dumped.get(
                "inputSchema", dumped.get("input_schema", {"type": "object"})
            )
            output_schema = dumped.get("outputSchema", dumped.get("output_schema"))
            annotations = dumped.get("annotations") or {}
            tools.append(
                ManagedMcpTool(
                    server.name,
                    tool_name,
                    str(
                        dumped.get("description")
                        or f"MCP tool {server.name}.{tool_name}"
                    ),
                    input_schema
                    if isinstance(input_schema, dict)
                    else {"type": "object"},
                    output_schema if isinstance(output_schema, dict) else None,
                    annotations if isinstance(annotations, dict) else {},
                )
            )
        return tuple(tools)

    async def _refresh_metadata(self, name: str) -> None:
        connection = self._sessions.get(name)
        if connection is None:
            return
        try:
            async with connection.lock:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await connection.session.list_tools()
                connection.tools = self._listed_tools(connection.server, response)
        except Exception as exc:
            self.errors[name] = str(exc) or type(exc).__name__

    @staticmethod
    def _listed_resources(
        server: McpServer, response: Any
    ) -> tuple[ManagedMcpResource, ...]:
        resources: list[ManagedMcpResource] = []
        for raw in getattr(response, "resources", ()) if response is not None else ():
            dumped = raw.model_dump(by_alias=True) if hasattr(raw, "model_dump") else {}
            uri = str(dumped.get("uri", getattr(raw, "uri", "")))
            if not uri:
                continue
            resources.append(
                ManagedMcpResource(
                    server.name,
                    uri,
                    str(dumped.get("name") or uri),
                    str(dumped["description"]) if dumped.get("description") else None,
                    str(dumped.get("mimeType") or dumped.get("mime_type"))
                    if dumped.get("mimeType") or dumped.get("mime_type")
                    else None,
                )
            )
        return tuple(resources)

    async def _refresh_resources(self, name: str) -> None:
        connection = self._sessions.get(name)
        if connection is None:
            return
        try:
            async with connection.lock:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await connection.session.list_resources()
                connection.resources = self._listed_resources(
                    connection.server, response
                )
        except Exception as exc:
            self.errors[name] = str(exc) or type(exc).__name__

    async def _disconnect(self, name: str) -> None:
        connection = self._sessions.pop(name, None)
        if connection is not None:
            await connection.stack.aclose()

    async def close(self) -> None:
        for name in tuple(self._sessions):
            await self._disconnect(name)


__all__ = ["McpManager"]
