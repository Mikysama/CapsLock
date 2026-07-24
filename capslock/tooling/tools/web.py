"""Web direct-capability tool execution."""

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


async def web_search(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    return await execute_action_tool(context, ActionType.WEB_SEARCH, arguments)


async def web_fetch(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    return await execute_action_tool(context, ActionType.WEB_FETCH, arguments)


def web_tools():
    from ..contracts import ResolvedToolPolicy, define_tool
    from .schemas import _schema, _str

    ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "web_search",
            "Search the Web through the durable approval workflow.",
            _schema({"query": _str()}, ["query"]),
            web_search,
        ),
        define_tool(
            "web_fetch",
            "Fetch a public HTTP URL through the durable approval workflow.",
            _schema({"url": _str()}, ["url"]),
            web_fetch,
        ),
    ]


__all__ = ["web_search", "web_fetch", "web_tools"]
