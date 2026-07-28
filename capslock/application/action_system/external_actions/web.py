"""Focused WebActionHandler implementation."""
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
from .transport import _preserve_manual_approval, _read_bounded


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
        url_validator: Any = None,
    ) -> None:
        self.sources = sources
        self.key = tavily_api_key
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.client_factory = client_factory or httpx.AsyncClient
        self.url_validator = url_validator or validate_public_url

    async def propose(
        self, action_type: ActionType, payload: dict[str, Any]
    ) -> ActionProposal:
        if action_type is ActionType.WEB_SEARCH:
            query = payload.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query must be a non-empty string")
            if not self.key:
                raise ValueError("Tavily API key is not configured")
            request = {"query": query}
            _preserve_manual_approval(payload, request)
            return ActionProposal(f"Search Tavily for: {query}", request)
        url = payload.get("url")
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        await asyncio.to_thread(self.url_validator, url)
        request = {"url": url}
        _preserve_manual_approval(payload, request)
        return ActionProposal(f"Fetch external URL: {url}", request)

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
        async with client.stream(
            "POST",
            TAVILY_SEARCH_URL,
            json={"query": query, "max_results": 8},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {self.key or ''}",
            },
        ) as response:
            response.raise_for_status()
            raw, truncated = await _read_bounded(response, self.max_bytes)
        if truncated:
            raise ValueError("Web search response exceeds the configured byte limit")
        payload = json.loads(raw.decode("utf-8"))
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
            self.url_validator, str(action.request["url"])
        )
        for _ in range(self.max_redirects + 1):
            async with client.stream(
                "GET",
                current,
                headers={
                    "Accept": "text/html,text/plain;q=0.9",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect response has no location")
                    current = await asyncio.to_thread(
                        self.url_validator, urljoin(current, location)
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
                encoding = response.encoding or "utf-8"
                raw, truncated = await _read_bounded(response, self.max_bytes)
                response_url = str(response.url)
            text = raw.decode(encoding, errors="replace")
            text = extract_text(text) if content_type == "text/html" else text
            source = await self.sources.add(
                session_id=action.session_id,
                run_id=action.run_id,
                url=response_url,
                title=response_url,
                excerpt=text[:8000],
                suspicious=is_suspicious(text),
            )
            return {
                "source_id": source.id,
                "url": source.url,
                "title": source.title,
                "excerpt": source.excerpt,
                "truncated": truncated,
                "untrusted": True,
                "suspicious": source.suspicious,
            }
        raise ValueError(f"too many redirects (limit {self.max_redirects})")

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("Web actions cannot be reversed")

    async def revalidate(self, action: ActionRecord) -> ActionProposal:
        return await self.propose(action.type, dict(action.request))
