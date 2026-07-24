"""Public SDK for sandboxed CapsLock tool plugins."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from ..plugins.manifest import PROTOCOL_VERSION


PluginCallable = Callable[..., object]


class CapabilityClient:
    """Request a scoped host capability over the bidirectional stdio channel."""

    def __init__(self, channel: "_Channel") -> None:
        self.channel = channel

    async def request(self, capability: str, **params: object) -> dict[str, object]:
        identifier = f"cap_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.channel.pending[identifier] = future
        await self.channel.send({
            "protocol_version": PROTOCOL_VERSION,
            "id": identifier,
            "method": "capability_request",
            "params": {"capability": capability, **params},
        })
        try:
            response = await future
        finally:
            self.channel.pending.pop(identifier, None)
        if response.get("ok") is not True:
            raise PermissionError(str(response.get("error", "capability denied")))
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("capability broker result must be an object")
        return result


class ProgressReporter:
    """Emit bounded, provider-neutral progress notifications to the host."""

    def __init__(self, request_id: object, channel: "_Channel") -> None:
        self.request_id = request_id
        self.channel = channel

    async def report(
        self, *, message: str, completed: float | None = None, total: float | None = None
    ) -> None:
        await self.channel.send({
            "protocol_version": PROTOCOL_VERSION,
            "method": "progress",
            "params": {
                "request_id": self.request_id,
                "message": str(message)[:4096],
                "completed": completed,
                "total": total,
            },
        })


class _Channel:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future[dict[str, object]]] = {}

    async def send(self, message: dict[str, object]) -> None:
        async with self.lock:
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    search_hint: str
    deferred: bool
    annotations: dict[str, object]
    capabilities: dict[str, list[str]]
    handler: PluginCallable


def serve_plugin(*, tools: list[ToolDefinition]) -> None:
    """Serve a plugin over the versioned JSONL/stdio protocol."""
    asyncio.run(_serve(tools))


async def _serve(tools: list[ToolDefinition]) -> None:
    catalog = {tool.name: tool for tool in tools}
    channel = _Channel()
    active: dict[object, asyncio.Task[None]] = {}

    async def respond(
        request_id: object,
        *,
        result: object | None = None,
        error: str | None = None,
    ) -> None:
        await channel.send(
            {
                "protocol_version": PROTOCOL_VERSION,
                "id": request_id,
                "ok": error is None,
                **({"result": result} if error is None else {"error": error}),
            }
        )

    async def call_tool(request_id: object, params: dict[str, object]) -> None:
        try:
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise ValueError("call_tool requires name and object arguments")
            tool = catalog.get(name)
            if tool is None:
                raise ValueError(f"unknown tool: {name}")
            parameter_count = len(inspect.signature(tool.handler).parameters)
            capability_client = CapabilityClient(channel)
            if parameter_count >= 3:
                value = tool.handler(
                    arguments,
                    capability_client,
                    ProgressReporter(request_id, channel),
                )
            elif parameter_count == 2:
                value = tool.handler(arguments, capability_client)
            else:
                value = tool.handler(arguments)
            if inspect.isawaitable(value):
                value = await value
            result = (
                value
                if isinstance(value, dict) and isinstance(value.get("ok"), bool)
                else {"ok": True, "data": value}
            )
            await respond(request_id, result=result)
        except asyncio.CancelledError:
            await respond(request_id, error="plugin tool call cancelled")
        except Exception as exc:
            await respond(request_id, error=str(exc) or type(exc).__name__)
        finally:
            active.pop(request_id, None)

    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline):
            request_id: object = None
            try:
                request = json.loads(line.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                request_id = request.get("id")
                if request.get("protocol_version") != PROTOCOL_VERSION:
                    raise ValueError("incompatible protocol version")
                capability = channel.pending.get(str(request_id))
                if capability is not None and request.get("method") is None:
                    if not capability.done():
                        capability.set_result(request)
                    continue
                method = request.get("method")
                params = request.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                if method == "initialize":
                    await respond(
                        request_id, result={"protocol_version": PROTOCOL_VERSION}
                    )
                elif method == "list_tools":
                    await respond(
                        request_id,
                        result={
                            "tools": [
                                {
                                    "name": tool.name,
                                    "description": tool.description,
                                    "input_schema": tool.input_schema,
                                    "output_schema": tool.output_schema,
                                    "search_hint": tool.search_hint,
                                    "deferred": tool.deferred,
                                    "annotations": tool.annotations,
                                    "capabilities": tool.capabilities,
                                }
                                for tool in tools
                            ]
                        },
                    )
                elif method == "call_tool":
                    if request_id in active:
                        raise ValueError("duplicate active request id")
                    active[request_id] = asyncio.create_task(
                        call_tool(request_id, params)
                    )
                elif method == "cancel":
                    target = params.get("request_id")
                    task = active.get(target)
                    if task is not None:
                        task.cancel()
                    await respond(
                        request_id,
                        result={"cancelled": task is not None, "request_id": target},
                    )
                else:
                    raise ValueError(f"unsupported method: {method}")
            except Exception as exc:
                await respond(request_id, error=str(exc) or type(exc).__name__)
    finally:
        tasks = tuple(active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "CapabilityClient",
    "ProgressReporter",
    "PROTOCOL_VERSION",
    "ToolDefinition",
    "serve_plugin",
]
