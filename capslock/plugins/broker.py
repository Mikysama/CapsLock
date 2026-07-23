"""Host-side capability broker for sandboxed plugin processes."""

from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..external import validate_public_url
from ..policy import PolicyError, WorkspacePolicy
from .manifest import PluginCapabilities


CapabilityCallback = Callable[[dict[str, Any]], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class BrokerCallbacks:
    workspace_write: CapabilityCallback | None = None
    network: CapabilityCallback | None = None
    process: CapabilityCallback | None = None
    credential: CapabilityCallback | None = None


class HostCapabilityBroker:
    def __init__(
        self,
        policy: WorkspacePolicy,
        grant: PluginCapabilities,
        *,
        callbacks: BrokerCallbacks = BrokerCallbacks(),
        max_read_bytes: int = 64 * 1024,
    ) -> None:
        self.policy = policy
        self.grant = grant
        self.callbacks = callbacks
        self.max_read_bytes = max_read_bytes
        self._secrets: set[str] = set()

    async def request(self, params: dict[str, Any]) -> dict[str, object]:
        kind = params.get("capability")
        if kind == "workspace_read":
            path = self._workspace_path(params, self.grant.workspace_read)
            content = await asyncio.to_thread(path.read_bytes)
            if len(content) > self.max_read_bytes:
                raise PolicyError("broker file read exceeds the size limit")
            return {
                "path": str(path.relative_to(self.policy.root)),
                "content": content.decode("utf-8", errors="replace"),
            }
        if kind == "workspace_write":
            self._workspace_path(params, self.grant.workspace_write, writing=True)
            return await self._callback(self.callbacks.workspace_write, params)
        if kind == "network":
            url = params.get("url")
            if not isinstance(url, str):
                raise PolicyError("network capability requires a URL")
            validated = await asyncio.to_thread(validate_public_url, url)
            host = urlsplit(validated).hostname or ""
            if not any(_host_matches(host, scope) for scope in self.grant.network_hosts):
                raise PolicyError("network host is outside the workspace grant")
            return await self._callback(self.callbacks.network, params)
        if kind == "process":
            template = params.get("template")
            if template not in self.grant.process_templates:
                raise PolicyError("process template is outside the workspace grant")
            return await self._callback(self.callbacks.process, params)
        if kind == "credential":
            name = params.get("name")
            if name not in self.grant.credentials:
                raise PolicyError("credential name is outside the workspace grant")
            result = await self._callback(self.callbacks.credential, {"name": name})
            value = result.get("value")
            if isinstance(value, str) and value:
                self._secrets.add(value)
            return result
        raise PolicyError("unsupported plugin capability request")

    def sanitize_text(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value

    def sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.sanitize_text(value)
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.sanitize(item) for item in value)
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        return value

    def _workspace_path(
        self,
        params: dict[str, Any],
        scopes: tuple[str, ...],
        *,
        writing: bool = False,
    ) -> Path:
        raw = params.get("path")
        if not isinstance(raw, str) or not any(fnmatch.fnmatch(raw, scope) for scope in scopes):
            raise PolicyError("workspace path is outside the workspace grant")
        return (
            self.policy.writable_file(raw, create=not self.policy.resolve(raw).exists())
            if writing
            else self.policy.readable_file(raw)
        )

    @staticmethod
    async def _callback(
        callback: CapabilityCallback | None, params: dict[str, Any]
    ) -> dict[str, object]:
        if callback is None:
            raise PolicyError("capability requires host approval and is unavailable")
        return await callback(params)


def _host_matches(host: str, scope: str) -> bool:
    expected = scope.rsplit(":", 1)[0] if ":" in scope else scope
    return host == expected or (
        expected.startswith("*.") and host.endswith(expected[1:])
    )
