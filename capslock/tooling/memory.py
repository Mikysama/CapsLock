"""Read-only memory tools exposed to the model."""

from __future__ import annotations

from typing import Any

from .core import RunContext, ToolResult


def search_memories(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    if context.memory is None:
        raise ValueError("memory storage is unavailable")
    query = arguments.get("query")
    limit = arguments.get("limit", 10)
    if not isinstance(query, str) or not isinstance(limit, int):
        raise ValueError("query must be a string and limit must be an integer")
    items = context.memory.search(query, run_id=context.run_id, limit=limit)
    data = [_for_model(item) for item in items]
    return ToolResult(
        True,
        data,
        memories=tuple(items),
        audit_data={"memory_ids": [item.id for item in items], "count": len(items)},
        audit_arguments={},
    )


def get_memory(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    if context.memory is None:
        raise ValueError("memory storage is unavailable")
    memory_id = arguments.get("memory_id")
    if not isinstance(memory_id, str):
        raise ValueError("memory_id must be a string")
    item = context.memory.get_for_model(memory_id, run_id=context.run_id)
    return ToolResult(
        True,
        _for_model(item),
        memories=(item,),
        audit_data={"memory_id": item.id, "revision": item.revision},
        audit_arguments={"memory_id": item.id},
    )


def _for_model(item: object) -> dict[str, object]:
    return {
        "memory_id": item.id,
        "content": item.content,
        "type": item.type.value,
        "scope": item.scope.value,
        "source": {"kind": item.source_kind, "ref": item.source_ref},
        "confidence": item.confidence,
        "expires_at": item.expires_at,
        "revision": item.revision,
        "citation": f"[[memory:{item.id}]]",
    }
