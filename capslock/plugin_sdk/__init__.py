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

    async def request(self, capability: str, **params: object) -> dict[str, object]:
        identifier = f"cap_{uuid.uuid4().hex}"
        message = {
            "protocol_version": PROTOCOL_VERSION,
            "id": identifier,
            "method": "capability_request",
            "params": {"capability": capability, **params},
        }
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            raise RuntimeError("host closed the capability broker channel")
        response = json.loads(line.decode("utf-8"))
        if (
            not isinstance(response, dict)
            or response.get("protocol_version") != PROTOCOL_VERSION
            or response.get("id") != identifier
        ):
            raise RuntimeError("invalid capability broker response")
        if response.get("ok") is not True:
            raise PermissionError(str(response.get("error", "capability denied")))
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("capability broker result must be an object")
        return result


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, object]
    handler: PluginCallable


def serve_plugin(*, tools: list[ToolDefinition]) -> None:
    """Serve a plugin over the versioned JSONL/stdio protocol."""
    asyncio.run(_serve(tools))


async def _serve(tools: list[ToolDefinition]) -> None:
    catalog = {tool.name: tool for tool in tools}
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        request_id: object = None
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            request_id = request.get("id")
            if request.get("protocol_version") != PROTOCOL_VERSION:
                raise ValueError("incompatible protocol version")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if method == "initialize":
                result = {"protocol_version": PROTOCOL_VERSION}
            elif method == "list_tools":
                result = {
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in tools
                    ]
                }
            elif method == "call_tool":
                name = params.get("name")
                arguments = params.get("arguments")
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise ValueError("call_tool requires name and object arguments")
                tool = catalog.get(name)
                if tool is None:
                    raise ValueError(f"unknown tool: {name}")
                if len(inspect.signature(tool.handler).parameters) >= 2:
                    value = tool.handler(arguments, CapabilityClient())
                else:
                    value = tool.handler(arguments)
                if inspect.isawaitable(value):
                    value = await value
                result = (
                    value
                    if isinstance(value, dict) and isinstance(value.get("ok"), bool)
                    else {"ok": True, "data": value}
                )
            else:
                raise ValueError(f"unsupported method: {method}")
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "id": request_id,
                "ok": True,
                "result": result,
            }
        except Exception as exc:
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": str(exc) or type(exc).__name__,
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


__all__ = [
    "CapabilityClient",
    "PROTOCOL_VERSION",
    "ToolDefinition",
    "serve_plugin",
]
