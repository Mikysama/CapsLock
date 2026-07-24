"""Git direct-capability tool execution."""

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


async def _git(context: ExecutionContext, *args: str) -> ToolOutcome:
    if not (context.policy.root / ".git").exists():
        return _outcome(False, {}, "workspace is not a Git repository")
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(context.policy.root),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        return _outcome(False, {}, "git command timed out")
    if process.returncode:
        return _outcome(
            False, {}, stderr.decode("utf-8", "replace").strip() or "git command failed"
        )
    return _outcome(True, {"output": stdout[:100000].decode("utf-8", "replace")})


async def git_status(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    return await _git(context, "status", "--short")


async def git_diff(context: ExecutionContext, arguments: dict[str, Any]) -> ToolOutcome:
    path = arguments.get("path")
    if path is not None:
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        context.policy.resolve(path)
        return await _git(context, "diff", "--", path)
    return await _git(context, "diff")


def git_tools():
    from ..contracts import ResolvedToolPolicy, define_tool
    from .schemas import _schema, _str

    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    safe_read = ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "git_status",
            "Show the Git working-tree status.",
            empty,
            git_status,
            policy=safe_read,
        ),
        define_tool(
            "git_diff",
            "Show the Git diff, optionally for one path.",
            _schema({"path": _str()}),
            git_diff,
            policy=safe_read,
        ),
    ]


__all__ = ["git_status", "git_diff", "git_tools"]
