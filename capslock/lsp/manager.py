"""Managed stdio Language Server Protocol clients."""

from __future__ import annotations

import asyncio
import platform
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ..configuration import LspServerSettings, LspSettings
from ..policy import WorkspacePolicy
from .discovery import autodetect_servers, find_root
from .sandbox import sandboxed_lsp_command
from .transport import encode_message, read_message


@dataclass
class _Server:
    name: str
    settings: LspServerSettings
    root: Path
    process: asyncio.subprocess.Process
    reader_task: asyncio.Task[None]
    pending: dict[int, asyncio.Future[object]]
    write_lock: asyncio.Lock
    opened: dict[Path, int]
    next_id: int = 1
    last_used: float = 0


class LspManager:
    def __init__(self, policy: WorkspacePolicy, settings: LspSettings) -> None:
        self.policy = policy
        self.settings = settings
        configured = settings.servers or {}
        self.servers = dict(configured)
        for name, candidate in autodetect_servers().items():
            if name not in self.servers and shutil.which(candidate.command[0]):
                self.servers[name] = candidate
        self.servers = (
            {
                name: server
                for name, server in self.servers.items()
                if server.command and shutil.which(server.command[0])
            }
            if settings.enabled
            else {}
        )
        system = platform.system()
        if (
            system == "Linux"
            and shutil.which("bwrap") is None
            or system == "Darwin"
            and shutil.which("sandbox-exec") is None
            or system not in {"Linux", "Darwin"}
        ):
            self.servers = {}
        self._running: dict[tuple[str, Path], _Server] = {}
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(self.servers)

    def supports(self, path: Path) -> bool:
        return any(
            path.suffix.casefold() in {item.casefold() for item in server.extensions}
            for server in self.servers.values()
        )

    async def request(
        self,
        path_text: str,
        method: str,
        params: dict[str, object],
    ) -> object:
        path = self.policy.readable_file(path_text)
        server = await self._server(path)
        await self._open(server, path)
        server.last_used = time.monotonic()
        identifier = server.next_id
        server.next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[object] = loop.create_future()
        server.pending[identifier] = future
        await self._send(
            server,
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params},
        )
        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                return await future
        except (asyncio.CancelledError, TimeoutError):
            await self._send(
                server,
                {
                    "jsonrpc": "2.0",
                    "method": "$/cancelRequest",
                    "params": {"id": identifier},
                },
            )
            raise
        finally:
            server.pending.pop(identifier, None)

    async def _server(self, path: Path) -> _Server:
        await self._reap_idle()
        matches = [
            (name, settings)
            for name, settings in self.servers.items()
            if path.suffix.casefold()
            in {item.casefold() for item in settings.extensions}
        ]
        if not matches:
            raise ValueError(f"no configured LSP server supports {path.suffix}")
        name, settings = sorted(matches)[0]
        root = find_root(path.parent, settings.root_markers)
        key = (name, root)
        async with self._lock:
            running = self._running.get(key)
            if running is not None and running.process.returncode is None:
                return running
            if running is not None:
                running.reader_task.cancel()
                await asyncio.gather(running.reader_task, return_exceptions=True)
            command = sandboxed_lsp_command(settings.command, root)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=root,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            pending: dict[int, asyncio.Future[object]] = {}
            placeholder = _Server(
                name,
                settings,
                root,
                process,
                None,  # type: ignore[arg-type]
                pending,
                asyncio.Lock(),
                {},
            )
            placeholder.reader_task = asyncio.create_task(self._reader(placeholder))
            self._running[key] = placeholder
            try:
                await self._request_raw(
                    placeholder,
                    "initialize",
                    {
                        "processId": None,
                        "rootUri": root.as_uri(),
                        "capabilities": {
                            "textDocument": {
                                "definition": {},
                                "references": {},
                                "hover": {},
                                "documentSymbol": {},
                                "implementation": {},
                                "callHierarchy": {},
                            }
                        },
                        "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                    },
                    self.settings.startup_timeout_seconds,
                )
                await self._send(
                    placeholder,
                    {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                )
            except BaseException:
                self._running.pop(key, None)
                await self._stop(placeholder, graceful=False)
                raise
            return placeholder

    async def _reap_idle(self) -> None:
        cutoff = time.monotonic() - self.settings.idle_timeout_seconds
        stale = [
            key
            for key, server in self._running.items()
            if server.last_used and server.last_used < cutoff
        ]
        for key in stale:
            server = self._running.pop(key)
            if server.process.returncode is None:
                server.process.terminate()
                try:
                    async with asyncio.timeout(2):
                        await server.process.wait()
                except TimeoutError:
                    server.process.kill()
                    await server.process.wait()
            server.reader_task.cancel()
            await asyncio.gather(server.reader_task, return_exceptions=True)

    async def _request_raw(
        self, server: _Server, method: str, params: object, timeout: float
    ) -> object:
        identifier = server.next_id
        server.next_id += 1
        future = asyncio.get_running_loop().create_future()
        server.pending[identifier] = future
        await self._send(
            server,
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params},
        )
        try:
            async with asyncio.timeout(timeout):
                return await future
        finally:
            server.pending.pop(identifier, None)

    async def _open(self, server: _Server, path: Path) -> None:
        path = path.resolve()
        if path in server.opened:
            return
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        language = server.name if server.name != "clang" else "cpp"
        await self._send(
            server,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": path.as_uri(),
                        "languageId": language,
                        "version": 1,
                        "text": text,
                    }
                },
            },
        )
        server.opened[path] = 1

    async def did_change(self, path_text: str | Path) -> None:
        """Notify servers that already opened a file after an applied Action."""
        path = Path(path_text)
        if not path.is_absolute():
            path = self.policy.resolve(str(path))
        path = path.resolve()
        matching = [
            server
            for server in self._running.values()
            if path in server.opened and server.process.returncode is None
        ]
        if not matching or not path.is_file():
            return
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        for server in matching:
            version = server.opened[path] + 1
            await self._send(
                server,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didChange",
                    "params": {
                        "textDocument": {"uri": path.as_uri(), "version": version},
                        "contentChanges": [{"text": text}],
                    },
                },
            )
            server.opened[path] = version

    async def _send(self, server: _Server, payload: dict[str, object]) -> None:
        if server.process.stdin is None:
            raise RuntimeError("LSP stdin is unavailable")
        async with server.write_lock:
            server.process.stdin.write(encode_message(payload))
            await server.process.stdin.drain()

    async def _reader(self, server: _Server) -> None:
        assert server.process.stdout is not None
        try:
            while True:
                message = await read_message(server.process.stdout)
                if not message:
                    continue
                identifier = message.get("id")
                if (
                    identifier in server.pending
                    and not server.pending[identifier].done()
                ):
                    future = server.pending[identifier]
                    if "error" in message:
                        future.set_exception(RuntimeError(str(message["error"])))
                    else:
                        future.set_result(message.get("result"))
        except BaseException as exc:
            for future in server.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(str(exc) or "LSP server stopped"))

    async def _stop(self, server: _Server, *, graceful: bool) -> None:
        if server.process.returncode is None and graceful:
            try:
                await self._request_raw(server, "shutdown", None, 2)
                await self._send(server, {"jsonrpc": "2.0", "method": "exit"})
            except Exception:
                server.process.terminate()
        elif server.process.returncode is None:
            server.process.terminate()
        if server.process.returncode is None:
            try:
                async with asyncio.timeout(2):
                    await server.process.wait()
            except TimeoutError:
                server.process.kill()
                await server.process.wait()
        server.reader_task.cancel()
        await asyncio.gather(server.reader_task, return_exceptions=True)

    async def switch_policy(self, policy: WorkspacePolicy) -> None:
        self.policy = policy
        await self.close()

    async def close(self) -> None:
        for server in list(self._running.values()):
            await self._stop(server, graceful=True)
        self._running.clear()
