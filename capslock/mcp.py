"""Scoped MCP configuration and approval-gated stdio calls."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .external import ExternalActionService
from .layout import ProjectLayout
from .policy import PolicyError, WorkspacePolicy
from .session import ExternalActionInfo, SessionStore


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


class McpRegistry:
    def __init__(self, policy: WorkspacePolicy, *, layout: ProjectLayout | None = None) -> None:
        self.policy = policy
        self.layout = layout or ProjectLayout.discover(policy.root)

    @property
    def project_path(self) -> Path:
        return self.layout.project_mcp

    @property
    def local_path(self) -> Path:
        return self.layout.local_mcp

    def servers(self) -> dict[str, McpServer]:
        project = self._load(self.project_path, private=False)
        local = self._load(self.local_path, private=True)
        names = set(project) | set(local)
        return {name: self._merge(name, project.get(name), local.get(name)) for name in names}

    def get(self, name: str) -> McpServer:
        server = self.servers().get(name)
        if server is None or not server.enabled:
            raise ValueError(f"MCP server is unavailable: {name}")
        return server

    def _load(self, path: Path, *, private: bool) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid MCP configuration: {path}") from exc
        servers = payload.get("servers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"MCP servers must be an object: {path}")
        output: dict[str, dict[str, Any]] = {}
        for name, value in servers.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                raise ValueError(f"invalid MCP server entry in {path}")
            if not private and "env" in value:
                raise PolicyError("project MCP configuration must not contain env or credentials")
            output[name] = value
        return output

    def _merge(self, name: str, project: dict[str, Any] | None, local: dict[str, Any] | None) -> McpServer:
        raw = {**(project or {}), **(local or {})}
        command = raw.get("command")
        args = raw.get("args", [])
        cwd = raw.get("cwd", ".")
        allowed = raw.get("allowed_tools", [])
        if not isinstance(command, str) or not command or not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"MCP server {name} requires command and string args")
        if not isinstance(cwd, str) or not isinstance(allowed, list) or not all(isinstance(tool, str) for tool in allowed):
            raise ValueError(f"MCP server {name} has invalid cwd or allowed_tools")
        self.policy.command_directory(cwd)
        project_allowed = project.get("allowed_tools") if project else None
        local_allowed = local.get("allowed_tools") if local else None
        if project_allowed is not None and local_allowed is not None:
            allowed = [tool for tool in project_allowed if tool in local_allowed]
        env = raw.get("env", {}) if local else {}
        if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
            raise ValueError(f"MCP server {name} has invalid local env")
        return McpServer(name, command, tuple(args), cwd, str(raw.get("description", "")), tuple(allowed), dict(env), bool(raw.get("enabled", True)), "local" if local else "project")


class McpService:
    def __init__(self, store: SessionStore, policy: WorkspacePolicy, session_id: str, run_id: str, emit, *, timeout_seconds: float = 30, output_limit_bytes: int = 100_000, layout: ProjectLayout | None = None) -> None:
        self.store, self.policy, self.session_id, self.run_id, self.emit = store, policy, session_id, run_id, emit
        self.timeout_seconds, self.output_limit_bytes = timeout_seconds, output_limit_bytes
        self.registry = McpRegistry(policy, layout=layout)
        self.actions = ExternalActionService(store, session_id, run_id, emit)

    def propose_connect(self, server: str) -> ExternalActionInfo:
        config = self.registry.get(server)
        return self.actions.propose("mcp_connect", {"server": config.name}, f"Start MCP server {config.name} and list its allowed tools")

    def propose_call(self, server: str, tool: str, arguments: dict[str, Any]) -> ExternalActionInfo:
        config = self.registry.get(server)
        if tool not in config.allowed_tools:
            raise PolicyError(f"MCP tool is not allowed for server {server}: {tool}")
        return self.actions.propose("mcp_call", {"server": server, "tool": tool, "arguments": arguments}, f"Call MCP {server}.{tool}")

    def execute(self, action_id: str) -> ExternalActionInfo:
        return asyncio.run(self.execute_async(action_id))

    async def execute_async(self, action_id: str) -> ExternalActionInfo:
        action = self.actions._action(action_id)
        if action.status != "approved":
            raise ValueError("external action requires explicit approval before execution")
        self.store.update_external_action(action.id, "running")
        try:
            result = await self._execute(action)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self.store.update_external_action(action.id, "failed", error=error)
            self.emit("external_action_finished", action_id=action.id, kind=action.kind, status="failed")
            return self.actions._action(action.id)
        self.store.update_external_action(action.id, "completed", result=result)
        self.emit("external_action_finished", action_id=action.id, kind=action.kind, status="completed")
        return self.actions._action(action.id)

    async def _execute(self, action: ExternalActionInfo) -> dict[str, Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError("MCP support requires the 'mcp' package; reinstall CapsLock dependencies") from exc
        server = self.registry.get(str(action.payload["server"]))
        env = {"PATH": os.environ.get("PATH", ""), **server.env}
        params = StdioServerParameters(command=server.command, args=list(server.args), env=env, cwd=str(self.policy.command_directory(server.cwd)))
        async with asyncio.timeout(self.timeout_seconds):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if action.kind == "mcp_connect":
                        tools = await session.list_tools()
                        items = [item.model_dump() if hasattr(item, "model_dump") else str(item) for item in getattr(tools, "tools", [])]
                        return {"server": server.name, "tools": [item for item in items if not isinstance(item, dict) or item.get("name") in server.allowed_tools]}
                    tool = str(action.payload["tool"])
                    if tool not in server.allowed_tools:
                        raise PolicyError(f"MCP tool is not allowed for server {server.name}: {tool}")
                    result = await session.call_tool(tool, action.payload["arguments"])
                    dumped = result.model_dump() if hasattr(result, "model_dump") else str(result)
                    serialized = json.dumps(dumped, ensure_ascii=False)
                    return {"server": server.name, "tool": tool, "result": json.loads(serialized[: self.output_limit_bytes]) if len(serialized) <= self.output_limit_bytes else {"text": serialized[: self.output_limit_bytes], "truncated": True}}
