"""Public SDK for CapsLock protocol-v1 local tool plugins."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..plugins.manifest import PROTOCOL_VERSION


PluginCallable = Callable[[dict[str, Any]], object]


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


__all__ = ["PROTOCOL_VERSION", "ToolDefinition", "serve_plugin"]
