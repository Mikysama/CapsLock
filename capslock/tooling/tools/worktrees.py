"""Worktrees direct-capability tool execution."""

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


async def create_worktree(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    return await execute_action_tool(context, ActionType.WORKTREE_CREATE, arguments)


async def exit_worktree(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    return await execute_action_tool(context, ActionType.WORKTREE_EXIT, arguments)


def worktree_tools():
    from ..contracts import InterruptBehavior, ResolvedToolPolicy, define_tool
    from .schemas import _schema, _str

    ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "create_worktree",
            "Create a session-owned Git worktree and switch the main session into it.",
            _schema({"name": _str()}),
            create_worktree,
            policy=ResolvedToolPolicy(
                context_mutation=True,
                external_side_effects=True,
                interrupt_behavior=InterruptBehavior.COMPLETE,
            ),
        ),
        define_tool(
            "exit_worktree",
            "Return to the control workspace, optionally removing the session-owned worktree.",
            _schema(
                {
                    "action": {"type": "string", "enum": ["keep", "remove"]},
                    "discard_changes": {"type": "boolean"},
                },
                ["action"],
            ),
            exit_worktree,
            policy=ResolvedToolPolicy(
                context_mutation=True,
                destructive=True,
                external_side_effects=True,
                interrupt_behavior=InterruptBehavior.COMPLETE,
            ),
        ),
    ]


__all__ = ["create_worktree", "exit_worktree", "worktree_tools"]
