"""Focused filesystem tool handlers."""

from __future__ import annotations

import asyncio  # noqa: F401
import base64  # noqa: F401
import fnmatch  # noqa: F401
import hashlib  # noqa: F401
import itertools  # noqa: F401
import json  # noqa: F401
import shutil  # noqa: F401
import uuid  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

from ....domain import ActionRecord, ActionStatus, ActionType  # noqa: F401
from ....evidence import Evidence  # noqa: F401
from ....security import TEXT_SUFFIXES  # noqa: F401
from ...contracts import (  # noqa: F401
    ExecutionContext,
    ToolContent,
    ToolExecution,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
)
from ..actions import execute_action_tool  # noqa: F401
from ..support import _outcome, _path  # noqa: F401


async def edit_file(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    _enforce_init_write(context, arguments, existing=True)
    return await execute_action_tool(context, ActionType.FILE_EDIT, arguments)


async def create_file(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    _enforce_init_write(context, arguments, existing=False)
    return await execute_action_tool(context, ActionType.FILE_CREATE, arguments)


async def write_file(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    path_text = _path(arguments)
    content = arguments.get("content")
    expected = arguments.get("expected_sha256")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if expected is not None and not isinstance(expected, str):
        raise ValueError("expected_sha256 must be a SHA-256 string or null")
    path = context.policy.resolve(path_text)
    _enforce_init_write(context, arguments, existing=path.exists())
    if path.exists():
        context.policy.writable_file(path_text)
        if expected is None:
            raise ValueError(
                "expected_sha256=null asserts that the file does not exist"
            )
        current_hash = hashlib.sha256(
            await asyncio.to_thread(path.read_bytes)
        ).hexdigest()
        if current_hash != expected:
            raise ValueError("file hash does not match expected_sha256")
        action_type = ActionType.FILE_EDIT
        payload = {
            "path": path_text,
            "replace_content": content,
            "expected_sha256": expected,
            "summary": arguments.get("summary"),
        }
    else:
        context.policy.writable_file(path_text, create=True)
        if expected is not None:
            raise ValueError("expected_sha256 must be null when creating a file")
        action_type = ActionType.FILE_CREATE
        payload = {
            "path": path_text,
            "content": content,
            "summary": arguments.get("summary"),
        }
    return await execute_action_tool(context, action_type, payload)


def _enforce_init_write(
    context: ExecutionContext,
    arguments: dict[str, Any],
    *,
    existing: bool,
) -> None:
    if context.runtime_state.get("init_run") is not True:
        return
    requested = _path(arguments)
    target = context.policy.resolve(requested)
    expected_target = context.policy.root / "CAPSLOCK.md"
    if target != expected_target:
        raise ValueError("/init may write only repository-root CAPSLOCK.md")
    if target.is_symlink():
        raise ValueError("/init refuses a symlink CAPSLOCK.md target")
    if not existing:
        if target.exists():
            raise ValueError("CAPSLOCK.md already exists; read and edit it")
        return
    state = context.runtime_state.get("init_state")
    recorded = state.get("capslock_sha256") if isinstance(state, dict) else None
    if not isinstance(recorded, str):
        raise ValueError("/init must read the existing CAPSLOCK.md before editing it")
    current = hashlib.sha256(target.read_bytes()).hexdigest()
    if current != recorded:
        raise ValueError("CAPSLOCK.md changed after /init read it; read it again")
    supplied = arguments.get("expected_sha256")
    if supplied is not None and supplied != recorded:
        raise ValueError("expected_sha256 does not match the /init read digest")
