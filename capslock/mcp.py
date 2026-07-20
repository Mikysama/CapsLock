"""Canonical project/local MCP configuration registry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .layout import ProjectLayout
from .policy import PolicyError, WorkspacePolicy


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
    def __init__(self, policy: WorkspacePolicy, *, layout: ProjectLayout) -> None:
        self.policy, self.layout = policy, layout

    def servers(self) -> dict[str, McpServer]:
        project = self._load(self.layout.project_mcp, private=False)
        local = self._load(self.layout.local_mcp, private=True)
        return {
            name: self._merge(name, project.get(name), local.get(name))
            for name in set(project) | set(local)
        }

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
        output = {}
        for name, value in servers.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                raise ValueError(f"invalid MCP server entry in {path}")
            if not private and "env" in value:
                raise PolicyError(
                    "project MCP configuration must not contain env or credentials"
                )
            output[name] = value
        return output

    def _merge(
        self, name: str, project: dict[str, Any] | None, local: dict[str, Any] | None
    ) -> McpServer:
        raw = {**(project or {}), **(local or {})}
        command, args, cwd, allowed = (
            raw.get("command"),
            raw.get("args", []),
            raw.get("cwd", "."),
            raw.get("allowed_tools", []),
        )
        if (
            not isinstance(command, str)
            or not command
            or not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
        ):
            raise ValueError(f"MCP server {name} requires command and string args")
        if (
            not isinstance(cwd, str)
            or not isinstance(allowed, list)
            or not all(isinstance(tool, str) for tool in allowed)
        ):
            raise ValueError(f"MCP server {name} has invalid cwd or allowed_tools")
        self.policy.command_directory(cwd)
        if (
            project
            and local
            and "allowed_tools" in project
            and "allowed_tools" in local
        ):
            allowed = [
                tool
                for tool in project["allowed_tools"]
                if tool in local["allowed_tools"]
            ]
        env = raw.get("env", {}) if local else {}
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in env.items()
        ):
            raise ValueError(f"MCP server {name} has invalid local env")
        return McpServer(
            name,
            command,
            tuple(args),
            cwd,
            str(raw.get("description", "")),
            tuple(allowed),
            dict(env),
            bool(raw.get("enabled", True)),
            "local" if local else "project",
        )
