"""Bounded stdio client for trusted local plugin processes."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
import inspect

from .manifest import PROTOCOL_VERSION, PluginManifest, PluginValidationError
from .sandbox import SandboxAdapter, SandboxUnavailableError, native_command


class PluginProtocolError(RuntimeError):
    pass


@dataclass
class _PluginSession:
    manifest: PluginManifest
    process: asyncio.subprocess.Process
    stderr_task: asyncio.Task[bytes]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)


class PluginProcessClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30,
        output_limit_bytes: int = 100_000,
        sandbox: SandboxAdapter | None = None,
        session_idle_seconds: float = 300,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes
        self.sandbox = sandbox if sandbox is not None else SandboxAdapter.detect()
        self.session_idle_seconds = max(1, session_idle_seconds)
        self._sessions: dict[str, _PluginSession] = {}
        self._session_lock = asyncio.Lock()

    async def verify(
        self, manifest: PluginManifest, *, trusted_native: bool = False
    ) -> dict[str, Any]:
        response = await self._exchange(
            manifest, "list_tools", {}, trusted_native=trusted_native
        )
        tools = response.get("tools")
        if not isinstance(tools, list):
            raise PluginProtocolError("plugin list_tools response is invalid")
        declared = {
            item.name: (item.input_schema, item.output_schema)
            for item in manifest.tools
        }
        reported = {
            str(item.get("name")): (item.get("input_schema"), item.get("output_schema"))
            for item in tools
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if declared != reported:
            raise PluginProtocolError("plugin runtime tools do not match the manifest")
        return response

    async def call(
        self,
        manifest: PluginManifest,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        trusted_native: bool = False,
        broker: Any = None,
        progress: Any = None,
    ) -> dict[str, Any]:
        if tool_name not in {item.name for item in manifest.tools}:
            raise PluginValidationError(f"plugin tool is not declared: {tool_name}")
        if manifest.lifecycle == "session":
            response = await self._session_call(
                manifest,
                tool_name,
                arguments,
                trusted_native=trusted_native,
                broker=broker,
                progress=progress,
            )
        else:
            response = await self._exchange(
                manifest,
                "call_tool",
                {"name": tool_name, "arguments": arguments},
                trusted_native=trusted_native,
                broker=broker,
                progress=progress,
            )
        if not isinstance(response.get("ok"), bool):
            raise PluginProtocolError("plugin tool response is invalid")
        return response

    async def _session_call(
        self,
        manifest: PluginManifest,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        trusted_native: bool,
        broker: Any,
        progress: Any,
    ) -> dict[str, Any]:
        session = await self._get_session(manifest, trusted_native=trusted_native)
        async with session.lock:
            if (
                session.process.returncode is not None
                or time.monotonic() - session.last_used > self.session_idle_seconds
            ):
                await self._drop_session(manifest.digest)
                session = await self._get_session(
                    manifest, trusted_native=trusted_native
                )
            process = session.process
            assert process.stdin is not None
            request_id = f"request_{uuid.uuid4().hex}"
            request = {
                "protocol_version": PROTOCOL_VERSION,
                "id": request_id,
                "method": "call_tool",
                "params": {"name": tool_name, "arguments": arguments},
            }
            try:
                process.stdin.write(
                    (json.dumps(request, ensure_ascii=False) + "\n").encode()
                )
                await process.stdin.drain()
                response = await self._read_response(
                    process,
                    broker=broker,
                    expected_id=request_id,
                    progress=progress,
                )
            except asyncio.CancelledError:
                await self._drop_session(manifest.digest, request_id=request_id)
                raise
            except Exception:
                await self._drop_session(manifest.digest)
                raise
            session.last_used = time.monotonic()
            if response.get("id") != request_id:
                await self._drop_session(manifest.digest)
                raise PluginProtocolError("plugin returned an unexpected response id")
            if response.get("ok") is False:
                raise PluginProtocolError(
                    _safe_plugin_text(
                        broker, str(response.get("error") or "plugin request failed")
                    )
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise PluginProtocolError("plugin result must be an object")
            return result

    async def _get_session(
        self, manifest: PluginManifest, *, trusted_native: bool
    ) -> _PluginSession:
        async with self._session_lock:
            existing = self._sessions.get(manifest.digest)
            if existing is not None:
                return existing
            process = await self._spawn(manifest, trusted_native=trusted_native)
            assert process.stdin is not None and process.stdout is not None
            stderr_task = asyncio.create_task(
                _capture_stderr(process, self.output_limit_bytes)
            )
            initialize = {
                "protocol_version": PROTOCOL_VERSION,
                "id": "initialize",
                "method": "initialize",
                "params": {
                    "plugin": manifest.name,
                    "version": manifest.version,
                    "digest": manifest.digest,
                    "lifecycle": "session",
                },
            }
            try:
                process.stdin.write(
                    (json.dumps(initialize, ensure_ascii=False) + "\n").encode()
                )
                await process.stdin.drain()
                response = await asyncio.wait_for(
                    _read_message(process.stdout, self.output_limit_bytes),
                    self.timeout_seconds,
                )
                if response.get("id") != "initialize" or response.get("ok") is not True:
                    raise PluginProtocolError("plugin initialization failed")
            except Exception:
                stderr_task.cancel()
                await _terminate(process)
                raise
            session = _PluginSession(manifest, process, stderr_task)
            self._sessions[manifest.digest] = session
            return session

    async def _spawn(
        self, manifest: PluginManifest, *, trusted_native: bool
    ) -> asyncio.subprocess.Process:
        if trusted_native:
            command = native_command(manifest)
        elif self.sandbox is None:
            raise SandboxUnavailableError(
                "plugin execution requires an OS sandbox backend"
            )
        else:
            command = self.sandbox.command(manifest)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "CAPSLOCK_PLUGIN_PROTOCOL": str(PROTOCOL_VERSION),
        }
        return await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=command.cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    async def _drop_session(
        self, digest: str, *, request_id: str | None = None
    ) -> None:
        session = self._sessions.pop(digest, None)
        if session is None:
            return
        process = session.process
        if request_id is not None and process.stdin is not None:
            with suppress(Exception):
                process.stdin.write(
                    (
                        json.dumps(
                            {
                                "protocol_version": PROTOCOL_VERSION,
                                "id": f"cancel_{request_id}",
                                "method": "cancel",
                                "params": {"request_id": request_id},
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await process.stdin.drain()
        await _terminate(process)
        session.stderr_task.cancel()
        await asyncio.gather(session.stderr_task, return_exceptions=True)

    async def close(self) -> None:
        for digest in tuple(self._sessions):
            await self._drop_session(digest)

    async def _exchange(
        self,
        manifest: PluginManifest,
        method: str,
        params: dict[str, Any],
        *,
        trusted_native: bool = False,
        broker: Any = None,
        progress: Any = None,
    ) -> dict[str, Any]:
        if trusted_native:
            command = native_command(manifest)
        elif self.sandbox is None:
            raise SandboxUnavailableError(
                "plugin execution requires an OS sandbox backend"
            )
        else:
            command = self.sandbox.command(manifest)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "CAPSLOCK_PLUGIN_PROTOCOL": str(PROTOCOL_VERSION),
        }
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=command.cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(
            process.stderr.read(self.output_limit_bytes + 1)
        )
        requests = [
            {
                "protocol_version": PROTOCOL_VERSION,
                "id": "initialize",
                "method": "initialize",
                "params": {
                    "plugin": manifest.name,
                    "version": manifest.version,
                    "digest": manifest.digest,
                },
            },
            {
                "protocol_version": PROTOCOL_VERSION,
                "id": "request",
                "method": method,
                "params": params,
            },
        ]
        try:
            for request in requests:
                process.stdin.write(
                    (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
                )
            await process.stdin.drain()
            initialize = await asyncio.wait_for(
                _read_message(process.stdout, self.output_limit_bytes),
                timeout=self.timeout_seconds,
            )
            if initialize.get("id") != "initialize" or initialize.get("ok") is not True:
                raise PluginProtocolError("plugin initialization failed")
            response = await self._read_response(
                process, broker=broker, expected_id="request", progress=progress
            )
            process.stdin.close()
            if response.get("id") != "request":
                raise PluginProtocolError("plugin returned an unexpected response id")
            await asyncio.wait_for(process.wait(), timeout=2)
            trailing = await process.stdout.read(self.output_limit_bytes + 1)
            if trailing:
                raise PluginProtocolError("plugin stdout contained extra protocol data")
            stderr = await stderr_task
            if len(stderr) > self.output_limit_bytes:
                raise PluginProtocolError("plugin stderr exceeded the output limit")
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise PluginProtocolError(
                    _safe_plugin_text(broker, detail)
                    or f"plugin exited with {process.returncode}"
                )
            if response.get("ok") is False:
                error = response.get("error")
                raise PluginProtocolError(
                    _safe_plugin_text(broker, str(error or "plugin request failed"))
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise PluginProtocolError("plugin result must be an object")
            return result
        except asyncio.CancelledError:
            stderr_task.cancel()
            try:
                process.stdin.write(
                    (
                        json.dumps(
                            {
                                "protocol_version": PROTOCOL_VERSION,
                                "id": "cancel",
                                "method": "cancel",
                                "params": {"request_id": "request"},
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await asyncio.shield(process.stdin.drain())
                await asyncio.sleep(0)
            except Exception:
                pass
            await _terminate(process)
            raise
        except asyncio.TimeoutError:
            stderr_task.cancel()
            await _terminate(process)
            raise PluginProtocolError("plugin request timed out") from None
        except Exception:
            stderr_task.cancel()
            await _terminate(process)
            raise

    async def _read_response(
        self, process, *, broker: Any, expected_id: str, progress: Any = None
    ):
        assert process.stdout is not None and process.stdin is not None
        while True:
            message = await asyncio.wait_for(
                _read_message(process.stdout, self.output_limit_bytes),
                timeout=self.timeout_seconds,
            )
            if message.get("method") == "progress":
                params = message.get("params")
                if progress is not None and isinstance(params, dict):
                    reported = progress(params)
                    if inspect.isawaitable(reported):
                        await reported
                continue
            if message.get("method") != "capability_request":
                return message
            request_id = message.get("id")
            params = message.get("params")
            if broker is None or not isinstance(params, dict):
                reply = {
                    "protocol_version": PROTOCOL_VERSION,
                    "id": request_id,
                    "ok": False,
                    "error": "capability broker is unavailable",
                }
            else:
                try:
                    result = await broker.request(params)
                    reply = {
                        "protocol_version": PROTOCOL_VERSION,
                        "id": request_id,
                        "ok": True,
                        "result": result,
                    }
                except Exception as exc:
                    reply = {
                        "protocol_version": PROTOCOL_VERSION,
                        "id": request_id,
                        "ok": False,
                        "error": str(exc) or type(exc).__name__,
                    }
            process.stdin.write(
                (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8")
            )
            await process.stdin.drain()


async def _read_message(
    stream: asyncio.StreamReader, output_limit_bytes: int
) -> dict[str, Any]:
    line = await stream.readline()
    if not line:
        raise PluginProtocolError("plugin closed stdout without a response")
    if len(line) > output_limit_bytes:
        raise PluginProtocolError("plugin response exceeded the output limit")
    try:
        document = json.loads(line.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PluginProtocolError(
            "plugin stdout contained a non-protocol message"
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("protocol_version") != PROTOCOL_VERSION
    ):
        raise PluginProtocolError(
            "plugin response has an incompatible protocol version"
        )
    return document


async def _capture_stderr(
    process: asyncio.subprocess.Process, output_limit_bytes: int
) -> bytes:
    assert process.stderr is not None
    captured = bytearray()
    while chunk := await process.stderr.read(8192):
        remaining = output_limit_bytes + 1 - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured)


def _safe_plugin_text(broker: Any, value: str) -> str:
    sanitizer = getattr(broker, "sanitize_text", None)
    return sanitizer(value) if callable(sanitizer) else value


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
