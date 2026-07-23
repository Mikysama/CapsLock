"""Async Web and MCP action handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
from ...plugins.broker import BrokerCallbacks
from ...ports import SourcePort
from .core import ActionExecution, ActionProposal
from .executors import McpStdioExecutor, PluginActionExecutor


class WebActionHandler:
    types = frozenset({ActionType.WEB_SEARCH, ActionType.WEB_FETCH})

    def __init__(
        self,
        sources: SourcePort,
        *,
        tavily_api_key: str | None,
        timeout_seconds: float,
        max_bytes: int,
        max_redirects: int,
        client_factory: Any = None,
    ) -> None:
        self.sources = sources
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
            source = await self.sources.add(
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
            source = await self.sources.add(
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

    async def revalidate(self, action: ActionRecord) -> ActionProposal:
        return await self.propose(action.type, dict(action.request))


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
        broker_callbacks: Callable[[ActionRecord], BrokerCallbacks] | None = None,
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
        self.plugin_executor = PluginActionExecutor(
            plugin_registry,
            self.plugin_client,
            output_limit_bytes=output_limit_bytes,
            policy=policy,
            broker_callbacks=broker_callbacks,
        )
        self.mcp_executor = McpStdioExecutor(
            policy,
            self.registry,
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
            if not entry.manifest.capabilities.contains(entry.granted_capabilities):
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
                    "capabilities": entry.granted_capabilities.as_dict(),
                    "trusted_native": entry.trusted_native,
                    "force_manual_approval": entry.trusted_native,
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
            result = await self.plugin_executor.execute(action)
        else:
            self.mcp_executor.registry = self.registry
            result = await self.mcp_executor.execute(action)
        return ActionExecution(result, ActionResultKind.SUCCESS)

    async def revalidate(self, action: ActionRecord) -> ActionProposal:
        return await self.propose(action.type, dict(action.request))

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("MCP actions cannot be reversed")
