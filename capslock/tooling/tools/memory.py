"""Memory direct-capability tool execution."""

from __future__ import annotations

import asyncio  # noqa: F401
import base64  # noqa: F401
import fnmatch  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import shutil  # noqa: F401
import uuid  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

from ...domain import ActionRecord, ActionStatus, ActionType  # noqa: F401
from ...evidence import Evidence  # noqa: F401
from ...security import TEXT_SUFFIXES  # noqa: F401
from ..contracts import (  # noqa: F401
    ExecutionContext,
    ToolContent,
    ToolExecution,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
)
from .actions import execute_action_tool  # noqa: F401
from .support import _outcome, _path  # noqa: F401


async def search_memories(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.memory is None:
        raise ValueError("memory storage is unavailable")
    query, limit = arguments.get("query"), arguments.get("limit", 10)
    if not isinstance(query, str) or not isinstance(limit, int):
        raise ValueError("query must be a string and limit must be an integer")
    items = await context.memory.search(query, run_id=context.run_id, limit=limit)
    return _outcome(
        True,
        [_memory_data(item) for item in items],
        memories=tuple(items),
        audit_data={"memory_ids": [item.id for item in items]},
        audit_arguments={},
    )


async def get_memory(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.memory is None:
        raise ValueError("memory storage is unavailable")
    identifier = arguments.get("memory_id")
    if not isinstance(identifier, str):
        raise ValueError("memory_id must be a string")
    item = await context.memory.get_for_model(identifier, run_id=context.run_id)
    return _outcome(
        True,
        _memory_data(item),
        memories=(item,),
        audit_data={"memory_id": item.id},
        audit_arguments={"memory_id": item.id},
    )


def _memory_data(item: Any) -> dict[str, object]:
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


def memory_tools():
    from ..contracts import ResolvedToolPolicy, define_tool
    from .schemas import _schema, _str

    safe_read = ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "search_memories",
            "Search user-managed memories.",
            _schema(
                {
                    "query": _str(),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["query"],
            ),
            search_memories,
            policy=safe_read,
        ),
        define_tool(
            "get_memory",
            "Read one visible memory.",
            _schema({"memory_id": _str()}, ["memory_id"]),
            get_memory,
            policy=safe_read,
        ),
    ]


__all__ = ["search_memories", "get_memory", "memory_tools"]
