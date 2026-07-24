"""Tasks direct-capability tool execution."""

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


def _task_data(item: Any) -> dict[str, object]:
    return {
        "task_id": item.id,
        "subject": item.subject,
        "description": item.description,
        "owner": item.owner,
        "active_form": item.active_form,
        "metadata": item.metadata or {},
        "status": item.status,
        "position": item.position,
        "blocked_by": list(item.blocked_by),
    }


async def create_task(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.tasks is None:
        raise ValueError("task storage is unavailable")
    item = await context.tasks.create(
        context.session_id,
        subject=str(arguments["subject"]),
        description=str(arguments.get("description", "")),
        blocked_by=list(arguments.get("blocked_by", [])),
        owner=arguments.get("owner"),
        run_id=context.run_id,
    )
    return ToolOutcome.success(_task_data(item))


async def list_tasks(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.tasks is None:
        raise ValueError("task storage is unavailable")
    items = await context.tasks.list(context.session_id, status=arguments.get("status"))
    return ToolOutcome.success({"tasks": [_task_data(item) for item in items]})


async def get_task(context: ExecutionContext, arguments: dict[str, Any]) -> ToolOutcome:
    if context.tasks is None:
        raise ValueError("task storage is unavailable")
    item = await context.tasks.get(
        str(arguments["task_id"]), session_id=context.session_id
    )
    if item is None:
        return ToolOutcome.failure(
            "task does not exist in this session", code="task_not_found"
        )
    return ToolOutcome.success(_task_data(item))


async def update_task(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.tasks is None:
        raise ValueError("task storage is unavailable")
    changes = {key: value for key, value in arguments.items() if key != "task_id"}
    item = await context.tasks.update(
        str(arguments["task_id"]), context.session_id, **changes
    )
    return ToolOutcome.success(_task_data(item))


def task_tools():
    from ..contracts import ResolvedToolPolicy, define_tool
    from .schemas import _schema, _str

    safe_read = ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "create_task",
            "Create one persistent session task, optionally blocked by other tasks.",
            _schema(
                {
                    "subject": _str(),
                    "description": _str(),
                    "blocked_by": {
                        "type": "array",
                        "items": _str(),
                        "uniqueItems": True,
                    },
                    "owner": _str(),
                },
                ["subject"],
            ),
            create_task,
            policy=ResolvedToolPolicy(context_mutation=True),
        ),
        define_tool(
            "list_tasks",
            "List persistent tasks in this session.",
            _schema(
                {
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "running",
                            "blocked",
                            "completed",
                            "failed",
                            "cancelled",
                        ],
                    },
                }
            ),
            list_tasks,
            policy=safe_read,
        ),
        define_tool(
            "get_task",
            "Read one persistent task in this session.",
            _schema({"task_id": _str()}, ["task_id"]),
            get_task,
            policy=safe_read,
        ),
        define_tool(
            "update_task",
            "Update fields, dependencies, or status of one persistent task.",
            _schema(
                {
                    "task_id": _str(),
                    "subject": _str(),
                    "description": _str(),
                    "owner": {"type": ["string", "null"]},
                    "active_form": {"type": ["string", "null"]},
                    "metadata": {"type": "object"},
                    "blocked_by": {
                        "type": "array",
                        "items": _str(),
                        "uniqueItems": True,
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "running",
                            "blocked",
                            "completed",
                            "failed",
                            "cancelled",
                        ],
                    },
                },
                ["task_id"],
            ),
            update_task,
            policy=ResolvedToolPolicy(context_mutation=True),
        ),
    ]


__all__ = ["create_task", "list_tasks", "get_task", "update_task", "task_tools"]
