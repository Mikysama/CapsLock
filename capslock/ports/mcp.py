"""Neutral managed MCP connection boundary and metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ..policy import WorkspacePolicy


@dataclass(frozen=True)
class McpServer:
    name: str
    command: str
    args: tuple[str, ...]
    cwd: str
    description: str
    allowed_tools: tuple[str, ...]
    env: dict[str, str]
    enabled: bool
    scope: str


@dataclass(frozen=True)
class ManagedMcpTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object] | None
    annotations: dict[str, object]


@dataclass(frozen=True)
class ManagedMcpResource:
    server: str
    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = None


class McpClientPort(Protocol):
    @property
    def errors(self) -> Mapping[str, str]: ...

    async def initialize(self) -> tuple[ManagedMcpTool, ...]: ...

    def server(self, name: str) -> McpServer: ...

    def tools(self) -> tuple[ManagedMcpTool, ...]: ...

    def resources(
        self, server: str | None = None
    ) -> tuple[ManagedMcpResource, ...]: ...

    async def refresh(self, server_name: str) -> tuple[ManagedMcpTool, ...]: ...

    async def call(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> object: ...

    async def read_resource(self, server_name: str, uri: str) -> object: ...

    async def switch_policy(self, policy: WorkspacePolicy) -> None: ...

    async def close(self) -> None: ...


__all__ = [
    "ManagedMcpResource",
    "ManagedMcpTool",
    "McpClientPort",
    "McpServer",
]
