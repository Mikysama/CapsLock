"""Native model-facing ToolDefinitions backed by managed MCP connections."""

from __future__ import annotations

import base64
import re
from dataclasses import replace
from typing import Any

from ...domain import ActionType
from ...ports import McpClientPort
from .actions import execute_action_tool
from ..contracts import (
    ExecutionContext,
    InterruptBehavior,
    ResolvedToolPolicy,
    ToolDefinition,
    ToolExecution,
    ToolContent,
    ToolOutcome,
    define_tool,
)


def mcp_tools(manager: McpClientPort) -> list[ToolDefinition]:
    result: list[ToolDefinition] = []
    for spec in manager.tools():
        public_name = f"mcp__{_name(spec.server)}__{_name(spec.name)}"

        async def execute(
            context: ExecutionContext,
            arguments: dict[str, Any],
            *,
            server: str = spec.server,
            tool: str = spec.name,
        ) -> ToolExecution:
            outcome = await execute_action_tool(
                context,
                ActionType.MCP_CALL,
                {"server": server, "tool": tool, "arguments": arguments},
            )
            if (
                isinstance(outcome, ToolOutcome)
                and outcome.ok
                and isinstance(outcome.data, dict)
            ):
                action_result = outcome.data.get("result")
                if isinstance(action_result, dict) and "result" in action_result:
                    actual = action_result["result"]
                    return replace(
                        outcome,
                        data=actual,
                        content=_mcp_content(actual),
                    )
            return outcome

        read_only = bool(
            spec.annotations.get(
                "readOnlyHint", spec.annotations.get("read_only", False)
            )
        )
        destructive = bool(
            spec.annotations.get(
                "destructiveHint", spec.annotations.get("destructive", False)
            )
        )
        open_world = bool(
            spec.annotations.get(
                "openWorldHint", spec.annotations.get("open_world", True)
            )
        )

        result.append(
            define_tool(
                public_name,
                spec.description,
                spec.input_schema,
                execute,
                output_schema=spec.output_schema,
                search_hint=f"MCP {spec.server} {spec.name}",
                deferred=True,
                policy=ResolvedToolPolicy(
                    read_only=read_only and not destructive,
                    destructive=destructive,
                    external_side_effects=not read_only or destructive,
                    open_world=open_world,
                    interrupt_behavior=InterruptBehavior.COMPLETE,
                    required_capabilities=frozenset({f"mcp:{spec.server}:{spec.name}"}),
                ),
            )
        )
    return result


def mcp_resource_tools(manager: McpClientPort) -> list[ToolDefinition]:
    async def list_resources(
        context: ExecutionContext, arguments: dict[str, Any]
    ) -> ToolOutcome:
        del context
        server = arguments.get("server")
        values = manager.resources(str(server) if server is not None else None)
        return ToolOutcome.success(
            {
                "resources": [
                    {
                        "server": item.server,
                        "uri": item.uri,
                        "name": item.name,
                        "description": item.description,
                        "mime_type": item.mime_type,
                    }
                    for item in values
                ],
                "errors": dict(manager.errors),
            }
        )

    async def read_resource(
        context: ExecutionContext, arguments: dict[str, Any]
    ) -> ToolOutcome:
        server, uri = str(arguments["server"]), str(arguments["uri"])
        raw = await manager.read_resource(server, uri)
        document = raw if isinstance(raw, dict) else {"contents": raw}
        contents = document.get("contents", [])
        if not isinstance(contents, list):
            contents = [contents]
        blocks: list[ToolContent] = []
        metadata: list[dict[str, object]] = []
        for item in contents:
            if not isinstance(item, dict):
                continue
            media_type = item.get("mimeType", item.get("mime_type"))
            if isinstance(item.get("text"), str):
                value = {
                    "server": server,
                    "uri": str(item.get("uri", uri)),
                    "text": item["text"],
                }
                blocks.append(
                    ToolContent.resource(
                        value, str(media_type) if media_type else "text/plain"
                    )
                )
                metadata.append({"uri": value["uri"], "kind": "text"})
            elif isinstance(item.get("blob"), str):
                try:
                    decoded = base64.b64decode(item["blob"], validate=True)
                except ValueError:
                    return ToolOutcome.failure(
                        "MCP resource returned invalid base64",
                        code="invalid_mcp_resource",
                    )
                if context.artifacts is None or context.invocation_id is None:
                    return ToolOutcome.failure(
                        "artifact storage is unavailable for binary MCP resource",
                        code="artifact_unavailable",
                    )
                artifact = await context.artifacts.put(
                    session_id=context.session_id,
                    run_id=context.run_id,
                    invocation_id=context.invocation_id,
                    content=decoded,
                    media_type=str(media_type)
                    if media_type
                    else "application/octet-stream",
                )
                descriptor = {
                    "artifact_id": artifact.id,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "uri": str(item.get("uri", uri)),
                }
                blocks.append(ToolContent.artifact(descriptor))
                metadata.append({**descriptor, "kind": "artifact"})
        return ToolOutcome.success(
            {"server": server, "uri": uri, "contents": metadata},
            content=tuple(blocks),
        )

    safe_read = ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "list_mcp_resources",
            "List resources advertised by connected MCP servers.",
            {
                "type": "object",
                "properties": {"server": {"type": "string"}},
                "additionalProperties": False,
            },
            list_resources,
            policy=safe_read,
            deferred=True,
            search_hint="MCP resources list data",
        ),
        define_tool(
            "read_mcp_resource",
            "Read one permitted MCP resource by server and URI.",
            {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "uri": {"type": "string"},
                },
                "required": ["server", "uri"],
                "additionalProperties": False,
            },
            read_resource,
            policy=safe_read,
            deferred=True,
            search_hint="read MCP resource URI text binary",
        ),
    ]


def _name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _mcp_content(value: object) -> tuple[ToolContent, ...]:
    if not isinstance(value, dict) or not isinstance(value.get("content"), list):
        return ()
    blocks: list[ToolContent] = []
    for raw in value["content"]:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "text" and isinstance(raw.get("text"), str):
            blocks.append(ToolContent.text(raw["text"]))
        elif kind == "image" and isinstance(raw.get("data"), str):
            media_type = str(raw.get("mimeType", "image/png"))
            blocks.append(
                ToolContent.image(f"data:{media_type};base64,{raw['data']}", media_type)
            )
        elif kind in {"resource", "resource_link"}:
            blocks.append(ToolContent.resource(raw, raw.get("mimeType")))
        else:
            blocks.append(ToolContent.json(raw))
    return tuple(blocks)


__all__ = ["mcp_tools"]
