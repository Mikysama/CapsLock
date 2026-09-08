"""Authenticated Unix-socket JSON-RPC bridge for explicit editor context."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..policy import WorkspacePolicy


@dataclass
class BridgeContext:
    active_file: str | None = None
    selection: dict[str, Any] | None = None
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


class IdeBridgeServer:
    protocol_version = 1

    def __init__(
        self,
        workspace: Path,
        state_root: Path,
        *,
        max_selection_bytes: int = 65_536,
        max_diagnostics: int = 500,
    ) -> None:
        self.workspace = workspace.resolve()
        self.policy = WorkspacePolicy(self.workspace)
        requested_state = state_root.expanduser()
        if requested_state.is_symlink():
            raise ValueError("bridge state root must not be a symbolic link")
        self.state_root = requested_state.resolve()
        if not self.state_root.is_relative_to(self.workspace):
            raise ValueError("bridge state root must be inside the workspace")
        self.socket_path = self.state_root / "bridge.sock"
        self.descriptor_path = self.state_root / "bridge.json"
        self.token = secrets.token_urlsafe(32)
        self.max_selection_bytes = max_selection_bytes
        self.max_diagnostics = max_diagnostics
        self.context = BridgeContext()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root.chmod(0o700)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            mode = self.socket_path.lstat().st_mode
            if not stat.S_ISSOCK(mode) and not stat.S_ISLNK(mode):
                raise RuntimeError("bridge socket path is occupied by a non-socket")
            self.socket_path.unlink()
        self.descriptor_path.unlink(missing_ok=True)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client, path=self.socket_path
            )
            self.socket_path.chmod(0o600)
            payload = {
                "protocol": self.protocol_version,
                "workspace": str(self.workspace),
                "socket": str(self.socket_path),
                "token": self.token,
                "pid": os.getpid(),
            }
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.descriptor_path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.socket_path.unlink(missing_ok=True)
        self.descriptor_path.unlink(missing_ok=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while line := await reader.readline():
                if len(line) > 1_000_000:
                    break
                try:
                    request = json.loads(line)
                    response = self.handle(request)
                except Exception as exc:
                    response = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32602, "message": str(exc)},
                    }
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        identifier = request.get("id")
        if request.get("jsonrpc") != "2.0" or request.get("token") != self.token:
            raise PermissionError("invalid bridge authentication")
        method = str(request.get("method", ""))
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("bridge params must be an object")
        if method == "initialize":
            result: object = {"protocol": self.protocol_version}
        elif method == "editor/context":
            result = self._update_context(params)
        elif method == "editor/status":
            result = {
                "active_file": self.context.active_file,
                "has_selection": self.context.selection is not None,
                "diagnostic_count": len(self.context.diagnostics),
            }
        else:
            raise ValueError(f"unsupported bridge method: {method}")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def _update_context(self, params: dict[str, Any]) -> dict[str, object]:
        active = self._path(params.get("active_file"))
        selection = params.get("selection")
        if selection is not None:
            if not isinstance(selection, dict):
                raise ValueError("selection must be an object")
            text = str(selection.get("text", ""))
            if len(text.encode("utf-8")) > self.max_selection_bytes:
                raise ValueError("selection exceeds the configured byte limit")
            selection_path = self._path(selection.get("path") or active)
            if selection_path is None:
                raise ValueError("selection path is required")
            start_line = max(1, int(selection.get("start_line", 1)))
            selection = {
                "path": selection_path,
                "text": text,
                "start_line": start_line,
                "end_line": max(start_line, int(selection.get("end_line", start_line))),
                "version": int(selection.get("version", 0)),
            }
        diagnostics = params.get("diagnostics", [])
        if not isinstance(diagnostics, list) or len(diagnostics) > self.max_diagnostics:
            raise ValueError("diagnostics exceed the configured item limit")
        safe_diagnostics = []
        for item in diagnostics:
            if not isinstance(item, dict):
                raise ValueError("diagnostic entries must be objects")
            message = str(item.get("message", ""))[:2000]
            diagnostic_path = self._path(item.get("path") or active)
            if diagnostic_path is None:
                raise ValueError("diagnostic path is required")
            safe_diagnostics.append(
                {
                    "path": diagnostic_path,
                    "line": max(1, int(item.get("line", 1))),
                    "severity": str(item.get("severity", "information"))[:32],
                    "message": message,
                }
            )
        self.context = BridgeContext(active, selection, safe_diagnostics)
        return {"accepted": True}

    def _path(self, value: object) -> str | None:
        if value in {None, ""}:
            return None
        raw = str(value)
        if raw.startswith("file:"):
            parsed = urlparse(raw)
            if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
                raise ValueError("only local file URIs are supported")
            raw = unquote(parsed.path)
        path = self.policy.resolve(raw)
        if not self.policy.is_agent_readable(path):
            raise ValueError("editor path is private to the workspace runtime")
        return str(path.relative_to(self.workspace))


__all__ = ["BridgeContext", "IdeBridgeServer"]
