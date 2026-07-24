"""Sources direct-capability tool execution."""

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


async def list_external_sources(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.sources is None:
        raise ValueError("source storage is unavailable")
    items = await context.sources.list(context.session_id)
    return _outcome(
        True,
        [
            {
                "source_id": item.id,
                "url": item.url,
                "title": item.title,
                "excerpt": item.excerpt,
                "fetched_at": item.fetched_at,
                "untrusted": True,
                "suspicious": item.suspicious,
            }
            for item in items
        ],
        source_ids=tuple(item.id for item in items),
    )


def source_tools():
    from ..contracts import ResolvedToolPolicy, define_tool

    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    safe_read = ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "list_external_sources",
            "List persisted untrusted Web sources.",
            empty,
            list_external_sources,
            policy=safe_read,
        ),
    ]


__all__ = ["list_external_sources", "source_tools"]
