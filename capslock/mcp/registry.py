"""Canonical project/local MCP configuration registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..credentials import resolve_credential
from ..external import validate_public_url
from ..layout import ProjectLayout
from ..policy import PolicyError, WorkspacePolicy
from ..ports.mcp import McpServer


class McpRegistry:
    def __init__(
        self,
        policy: WorkspacePolicy,
        *,
        layout: ProjectLayout,
        remote_enabled: bool = True,
    ) -> None:
        self.policy, self.layout = policy, layout
        self.remote_enabled = remote_enabled

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
            if not private and ({"env", "headers"} & set(value)):
                raise PolicyError(
                    "project MCP configuration must not contain env, headers, or credentials"
                )
            output[name] = value
        return output

    def _merge(
        self, name: str, project: dict[str, Any] | None, local: dict[str, Any] | None
    ) -> McpServer:
        raw = {**(project or {}), **(local or {})}
        transport = str(raw.get("transport", "stdio"))
        if transport not in {"stdio", "streamable_http", "sse"}:
            raise ValueError(f"MCP server {name} has invalid transport")
        if transport != "stdio" and not self.remote_enabled:
            raise PolicyError("remote MCP transport is disabled")
        command, args, cwd, allowed = (
            raw.get("command", ""),
            raw.get("args", []),
            raw.get("cwd", "."),
            raw.get("allowed_tools", []),
        )
        if (
            not isinstance(command, str)
            or (transport == "stdio" and not command)
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
        if transport == "stdio":
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
        resolved_env = {}
        for key, value in env.items():
            if value.startswith(("env:", "keyring:")):
                resolved = resolve_credential(value)
                if not resolved:
                    raise ValueError(
                        f"MCP server {name} credential for {key} is missing"
                    )
                resolved_env[key] = resolved
            else:
                resolved_env[key] = value
        url = raw.get("url")
        headers = raw.get("headers", {}) if local else {}
        if transport != "stdio":
            if not isinstance(url, str) or not url:
                raise ValueError(f"MCP server {name} requires a URL")
            if not url.startswith("https://"):
                raise PolicyError("remote MCP URLs must use HTTPS")
            validate_public_url(url)
            if not isinstance(headers, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in headers.items()
            ):
                raise ValueError(f"MCP server {name} has invalid local headers")
            resolved_headers = {}
            for key, value in headers.items():
                resolved = (
                    resolve_credential(value)
                    if value.startswith(("env:", "keyring:"))
                    else value
                )
                if key.casefold() in {"authorization", "proxy-authorization"} and not value.startswith(("env:", "keyring:")):
                    raise PolicyError("remote MCP authorization must use a credential reference")
                if not resolved:
                    raise ValueError(f"MCP server {name} credential for {key} is missing")
                resolved_headers[key] = resolved
        else:
            resolved_headers = {}
        return McpServer(
            name=name,
            command=command,
            args=tuple(args),
            cwd=cwd,
            description=str(raw.get("description", "")),
            allowed_tools=tuple(allowed),
            env=resolved_env,
            enabled=bool(raw.get("enabled", True)),
            scope="local" if local else "project",
            transport=transport,
            url=str(url) if url else None,
            headers=resolved_headers,
        )
