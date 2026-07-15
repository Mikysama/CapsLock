"""Approval-gated Web research with source persistence and SSRF protections."""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from .policy import PolicyError
from .session import ExternalActionInfo, SessionStore

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_INJECTION = re.compile(r"(?i)(ignore (previous|all) instructions|system prompt|you are now|tool[_ ]?call|assistant message)")


class ExternalActionService:
    def __init__(self, store: SessionStore, session_id: str, run_id: str, emit) -> None:
        self.store, self.session_id, self.run_id, self.emit = store, session_id, run_id, emit

    def propose(self, kind: str, payload: dict[str, object], summary: str) -> ExternalActionInfo:
        action = self.store.create_external_action(session_id=self.session_id, run_id=self.run_id, kind=kind, payload=payload, summary=summary)
        self.emit("external_action_proposed", action_id=action.id, kind=kind)
        return action

    def approve(self, action_id: str) -> ExternalActionInfo:
        action = self._action(action_id)
        if action.status != "pending":
            raise ValueError(f"action is not pending: {action.status}")
        self.store.update_external_action(action.id, "approved")
        self.emit("external_action_approved", action_id=action.id, kind=action.kind)
        return self._action(action.id)

    def reject(self, action_id: str) -> ExternalActionInfo:
        action = self._action(action_id)
        if action.status != "pending":
            raise ValueError(f"action is not pending: {action.status}")
        self.store.update_external_action(action.id, "rejected")
        self.emit("external_action_rejected", action_id=action.id, kind=action.kind)
        return self._action(action.id)

    def _action(self, action_id: str) -> ExternalActionInfo:
        action = self.store.get_external_action(action_id, session_id=self.session_id)
        if action is None:
            raise PolicyError("external action does not belong to this session or does not exist")
        return action


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def extract_text(body: str) -> str:
    parser = _TextExtractor()
    parser.feed(body)
    return " ".join(" ".join(parser.parts).split())


def is_suspicious(text: str) -> bool:
    return bool(_INJECTION.search(text))


def validate_public_url(url: str, resolver=socket.getaddrinfo) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PolicyError("URL must use http or https with a hostname")
    hostname = parsed.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise PolicyError("localhost URLs are not allowed")
    try:
        records = resolver(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PolicyError(f"URL hostname cannot be resolved: {hostname}") from exc
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_unspecified:
            raise PolicyError("URLs resolving to non-public addresses are not allowed")
    return url


class WebService:
    def __init__(self, store: SessionStore, session_id: str, run_id: str, emit, *, tavily_api_key: str | None, timeout_seconds: float = 20, max_bytes: int = 500_000, max_redirects: int = 3, client: httpx.Client | None = None) -> None:
        self.store, self.session_id, self.run_id, self.emit = store, session_id, run_id, emit
        self.key, self.timeout_seconds, self.max_bytes, self.max_redirects = tavily_api_key, timeout_seconds, max_bytes, max_redirects
        self.client = client
        self._owns_client = False
        self.actions = ExternalActionService(store, session_id, run_id, emit)

    def propose_search(self, query: str) -> ExternalActionInfo:
        if not query.strip():
            raise ValueError("search query must not be empty")
        if not self.key:
            raise ValueError("Tavily API key is not configured")
        return self.actions.propose("web_search", {"query": query}, f"Search Tavily for: {query}")

    def propose_fetch(self, url: str) -> ExternalActionInfo:
        validate_public_url(url)
        return self.actions.propose("web_fetch", {"url": url}, f"Fetch external URL: {url}")

    def execute(self, action_id: str) -> ExternalActionInfo:
        try:
            return self._execute_action(action_id)
        finally:
            self.close()

    def _execute_action(self, action_id: str) -> ExternalActionInfo:
        action = self.actions._action(action_id)
        if action.status != "approved":
            raise ValueError("external action requires explicit approval before execution")
        self.store.update_external_action(action.id, "running")
        try:
            result = self._search(str(action.payload["query"])) if action.kind == "web_search" else self._fetch(str(action.payload["url"]))
        except Exception as exc:
            self.store.update_external_action(action.id, "failed", error=str(exc))
            self.emit("external_action_finished", action_id=action.id, kind=action.kind, status="failed")
            return self.actions._action(action.id)
        self.store.update_external_action(action.id, "completed", result=result)
        self.emit("external_action_finished", action_id=action.id, kind=action.kind, status="completed")
        return self.actions._action(action.id)

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()
            self.client = None
            self._owns_client = False

    def _client(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout_seconds, follow_redirects=False)
            self._owns_client = True
        return self.client

    def _search(self, query: str) -> dict[str, object]:
        response = self._client().post(
            TAVILY_SEARCH_URL,
            json={"query": query, "max_results": 8},
            headers={"Accept": "application/json", "Authorization": f"Bearer {self.key or ''}"},
        )
        response.raise_for_status()
        payload = response.json()
        results: list[dict[str, object]] = []
        for rank, item in enumerate(payload.get("results", [])[:8], start=1):
            url, title, content = item.get("url"), item.get("title", ""), item.get("content", "")
            if not isinstance(url, str):
                continue
            source = self.store.add_source(session_id=self.session_id, run_id=self.run_id, url=url, title=str(title), excerpt=str(content)[:4_000], suspicious=is_suspicious(str(content)))
            results.append({"rank": rank, "source_id": source.id, "url": source.url, "title": source.title, "excerpt": source.excerpt, "suspicious": source.suspicious})
        return {"query": query, "results": results}

    def _fetch(self, url: str) -> dict[str, object]:
        current = validate_public_url(url)
        for _ in range(self.max_redirects + 1):
            response = self._client().get(current, headers={"Accept": "text/html,text/plain;q=0.9"})
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirect response has no location")
                current = validate_public_url(urljoin(current, location))
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            if content_type not in {"text/html", "text/plain"}:
                raise ValueError(f"unsupported fetched content type: {content_type or 'unknown'}")
            raw = response.content[: self.max_bytes]
            text = raw.decode(response.encoding or "utf-8", errors="replace")
            text = extract_text(text) if content_type == "text/html" else text
            excerpt = text[:8_000]
            source = self.store.add_source(session_id=self.session_id, run_id=self.run_id, url=str(response.url), title=str(response.url), excerpt=excerpt, suspicious=is_suspicious(text))
            return {"source_id": source.id, "url": source.url, "title": source.title, "excerpt": source.excerpt, "truncated": len(response.content) > self.max_bytes, "untrusted": True, "suspicious": source.suspicious}
        raise ValueError(f"too many redirects (limit {self.max_redirects})")
