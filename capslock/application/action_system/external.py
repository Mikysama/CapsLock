"""Async Web and MCP action handlers."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import urljoin

import httpx

from ...domain import ActionRecord, ActionResultKind, ActionType
from ...external import (
    TAVILY_SEARCH_URL,
    extract_text,
    is_suspicious,
    validate_public_url,
)
from ...layout import ProjectLayout
from ...mcp import McpRegistry
from ...policy import PolicyError, WorkspacePolicy
from ...plugins import PluginProcessClient, PluginRegistry
from ...storage.repositories_v2 import WorkspaceRepositories
from .core import ActionExecution, ActionProposal


class WebActionHandler:
    types = frozenset({ActionType.WEB_SEARCH, ActionType.WEB_FETCH})

    def __init__(
        self,
        repositories: WorkspaceRepositories,
        *,
        tavily_api_key: str | None,
        timeout_seconds: float,
        max_bytes: int,
        max_redirects: int,
        client_factory: Any = None,
    ) -> None:
        self.repositories = repositories
        self.key = tavily_api_key
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.client_factory = client_factory or httpx.AsyncClient

    async def propose(
        self, action_type: ActionType, payload: dict[str, Any]
    ) -> ActionProposal:
        if action_type is ActionType.WEB_SEARCH:
            query = payload.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query must be a non-empty string")
            if not self.key:
                raise ValueError("Tavily API key is not configured")
            return ActionProposal(f"Search Tavily for: {query}", {"query": query})
        url = payload.get("url")
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        await asyncio.to_thread(validate_public_url, url)
        return ActionProposal(f"Fetch external URL: {url}", {"url": url})

    async def execute(self, action: ActionRecord) -> ActionExecution:
        async with self.client_factory(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            if action.type is ActionType.WEB_SEARCH:
                result = await self._search(client, action)
            else:
                result = await self._fetch(client, action)
        return ActionExecution(result, ActionResultKind.SUCCESS)

    async def _search(
        self, client: httpx.AsyncClient, action: ActionRecord
    ) -> dict[str, object]:
        query = str(action.request["query"])
        response = await client.post(
            TAVILY_SEARCH_URL,
            json={"query": query, "max_results": 8},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.key or ''}",
            },
        )
        response.raise_for_status()
        payload = response.json()
        results: list[dict[str, object]] = []
        for rank, item in enumerate(payload.get("results", [])[:8], start=1):
            url, title, content = (
                item.get("url"),
                item.get("title", ""),
                item.get("content", ""),
            )
            if not isinstance(url, str):
                continue
            source = await self.repositories.sources.add(
                session_id=action.session_id,
                run_id=action.run_id,
                url=url,
                title=str(title),
                excerpt=str(content)[:4000],
                suspicious=is_suspicious(str(content)),
            )
            results.append(
                {
                    "rank": rank,
                    "source_id": source.id,
                    "url": source.url,
                    "title": source.title,
                    "excerpt": source.excerpt,
                    "suspicious": source.suspicious,
                }
            )
        return {"query": query, "results": results}

    async def _fetch(
        self, client: httpx.AsyncClient, action: ActionRecord
    ) -> dict[str, object]:
        current = await asyncio.to_thread(
            validate_public_url, str(action.request["url"])
        )
        for _ in range(self.max_redirects + 1):
            response = await client.get(
                current, headers={"Accept": "text/html,text/plain;q=0.9"}
            )
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirect response has no location")
                current = await asyncio.to_thread(
                    validate_public_url, urljoin(current, location)
                )
                continue
            response.raise_for_status()
            content_type = (
                response.headers.get("content-type", "").split(";", 1)[0].casefold()
            )
            if content_type not in {"text/html", "text/plain"}:
                raise ValueError(
                    f"unsupported fetched content type: {content_type or 'unknown'}"
                )
            raw = response.content[: self.max_bytes]
            text = raw.decode(response.encoding or "utf-8", errors="replace")
            text = extract_text(text) if content_type == "text/html" else text
            source = await self.repositories.sources.add(
                session_id=action.session_id,
                run_id=action.run_id,
                url=str(response.url),
                title=str(response.url),
                excerpt=text[:8000],
                suspicious=is_suspicious(text),
            )
            return {
                "source_id": source.id,
                "url": source.url,
                "title": source.title,
                "excerpt": source.excerpt,
                "truncated": len(response.content) > self.max_bytes,
                "untrusted": True,
                "suspicious": source.suspicious,
            }
        raise ValueError(f"too many redirects (limit {self.max_redirects})")

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("Web actions cannot be reversed")


class McpActionHandler:
    types = frozenset({ActionType.MCP_CONNECT, ActionType.MCP_CALL})

    def __init__(
        self,
        policy: WorkspacePolicy,
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
        layout: ProjectLayout,
        plugin_registry: PluginRegistry | None = None,
        plugin_client: PluginProcessClient | None = None,
    ) -> None:
        self.policy = policy
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes
        self.registry = McpRegistry(policy, layout=layout)
        self.plugin_registry = plugin_registry
        self.plugin_client = plugin_client or PluginProcessClient(
            timeout_seconds=timeout_seconds,
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
            if not entry.manifest.permissions.issubset(entry.granted_permissions):
                raise PolicyError("plugin workspace permission grant is incomplete")
            tool, arguments = payload.get("tool"), payload.get("arguments")
            if not isinstance(tool, str) or not isinstance(arguments, dict):
                raise ValueError("tool and arguments must be provided")
            if tool not in {item.name for item in entry.manifest.tools}:
                raise PolicyError(f"plugin tool is not declared: {plugin_name}.{tool}")
            return ActionProposal(
                f"Call plugin {plugin_name}.{tool}",
                {
                    "plugin": plugin_name,
                    "tool": tool,
                    "arguments": arguments,
                    "digest": entry.manifest.digest,
                    "permissions": sorted(
                        item.value for item in entry.manifest.permissions
                    ),
                },
            )
        server_name = payload.get("server")
        if not isinstance(server_name, str):
            raise ValueError("server must be a string")
        server = await asyncio.to_thread(self.registry.get, server_name)
        if action_type is ActionType.MCP_CONNECT:
            return ActionProposal(
                f"Start MCP server {server.name} and list its allowed tools",
                {"server": server.name},
            )
        tool, arguments = payload.get("tool"), payload.get("arguments")
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            raise ValueError("tool and arguments must be provided")
        if tool not in server.allowed_tools:
            raise PolicyError(
                f"MCP tool is not allowed for server {server.name}: {tool}"
            )
        return ActionProposal(
            f"Call MCP {server.name}.{tool}",
            {"server": server.name, "tool": tool, "arguments": arguments},
        )

    async def execute(self, action: ActionRecord) -> ActionExecution:
        plugin_name = action.request.get("plugin")
        if plugin_name is not None:
            if not isinstance(plugin_name, str) or self.plugin_registry is None:
                raise ValueError("plugin support is unavailable")
            entry = await asyncio.to_thread(self.plugin_registry.get, plugin_name)
            if not entry.manifest.permissions.issubset(entry.granted_permissions):
                raise PolicyError("plugin workspace permission grant is incomplete")
            if action.request.get("digest") != entry.manifest.digest:
                raise PolicyError(
                    "plugin package or workspace grant changed after approval"
                )
            response = await self.plugin_client.call(
                entry.manifest,
                str(action.request["tool"]),
                action.request["arguments"],
            )
            result = {
                "plugin": plugin_name,
                "tool": action.request["tool"],
                "result": response.get("data"),
                "plugin_ok": response.get("ok"),
                "plugin_error": response.get("error"),
                "untrusted": True,
            }
            encoded = json.dumps(result, ensure_ascii=False, default=str)
            if len(encoded.encode("utf-8")) > self.output_limit_bytes:
                result = {
                    "text": encoded.encode()[: self.output_limit_bytes].decode(
                        "utf-8", "ignore"
                    ),
                    "truncated": True,
                    "untrusted": True,
                }
            return ActionExecution(result, ActionResultKind.SUCCESS)
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError("MCP support requires the mcp package") from exc
        server = await asyncio.to_thread(
            self.registry.get, str(action.request["server"])
        )
        env = {"PATH": os.environ.get("PATH", ""), **server.env}
        params = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env=env,
            cwd=str(self.policy.command_directory(server.cwd)),
        )
        async with asyncio.timeout(self.timeout_seconds):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if action.type is ActionType.MCP_CONNECT:
                        response = await session.list_tools()
                        tools = [
                            item.model_dump()
                            if hasattr(item, "model_dump")
                            else str(item)
                            for item in getattr(response, "tools", [])
                        ]
                        result: dict[str, Any] = {
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
            result = {
                "text": encoded.encode()[: self.output_limit_bytes].decode(
                    "utf-8", "ignore"
                ),
                "truncated": True,
            }
        else:
            result = json.loads(encoded)
        return ActionExecution(result, ActionResultKind.SUCCESS)

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("MCP actions cannot be reversed")
