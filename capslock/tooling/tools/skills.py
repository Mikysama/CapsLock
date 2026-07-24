"""Skills direct-capability tool execution."""

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


async def load_skill(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    name = arguments.get("name")
    if not isinstance(name, str) or context.skills is None:
        raise ValueError("Skill name or loader is unavailable")
    data, audit = await asyncio.to_thread(
        context.skills.load_data, context.run_id, name, trigger="model"
    )
    return _outcome(True, data, audit_data=audit)


async def read_skill_resource(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    name, path = arguments.get("name"), arguments.get("path")
    start_line, end_line = arguments.get("start_line", 1), arguments.get("end_line")
    if (
        not isinstance(name, str)
        or not isinstance(path, str)
        or not isinstance(start_line, int)
        or end_line is not None
        and not isinstance(end_line, int)
        or context.skills is None
    ):
        raise ValueError("invalid Skill resource request")
    data, audit = await asyncio.to_thread(
        context.skills.read_resource,
        context.run_id,
        name,
        path,
        start_line=start_line,
        end_line=end_line,
    )
    return _outcome(True, data, audit_data=audit)


def skill_tools():
    from ..contracts import ResolvedToolPolicy, define_tool
    from .schemas import _int, _schema, _str

    ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "load_skill",
            "Load matching local Skill instructions.",
            _schema({"name": _str()}, ["name"]),
            load_skill,
        ),
        define_tool(
            "read_skill_resource",
            "Read a loaded Skill text resource.",
            _schema(
                {
                    "name": _str(),
                    "path": _str(),
                    "start_line": _int(),
                    "end_line": _int(),
                },
                ["name", "path"],
            ),
            read_skill_resource,
        ),
    ]


__all__ = ["load_skill", "read_skill_resource", "skill_tools"]
