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


async def list_files(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    def read() -> list[str]:
        directory = context.policy.readable_directory(_path(arguments))
        pattern = arguments.get("pattern", "*")
        if not isinstance(pattern, str):
            raise ValueError("pattern must be a string")
        return [
            str(item.relative_to(context.policy.root))
            for item in sorted(directory.rglob("*"))
            if item.is_file()
            and context.policy.is_agent_readable(item)
            and fnmatch.fnmatch(item.name, pattern)
        ][: context.policy.max_files]

    files = await asyncio.to_thread(read)
    return _outcome(
        True, {"path": _path(arguments), "files": files, "count": len(files)}
    )


async def read_file(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    def read() -> tuple[Path, Evidence, str, int]:
        path = context.policy.readable_file(_path(arguments))
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError(f"unsupported text file type: {path.suffix or '(none)'}")
        lines = path.read_text(encoding="utf-8").splitlines()
        start, end = (
            int(arguments.get("start_line", 1)),
            int(arguments.get("end_line", len(lines))),
        )
        if start < 1 or end < start:
            raise ValueError("line range must satisfy 1 <= start_line <= end_line")
        end = min(end, len(lines))
        return (
            path,
            Evidence(path, start, end, "\n".join(lines[start - 1 : end])),
            hashlib.sha256(path.read_bytes()).hexdigest(),
            len(lines),
        )

    path, passage, digest, total_lines = await asyncio.to_thread(read)
    return _outcome(
        True,
        {
            "path": str(path),
            "start_line": passage.start_line,
            "end_line": passage.end_line,
            "text": passage.text,
            "evidence_id": passage.id,
            "sha256": digest,
            "total_lines": total_lines,
        },
        citations=(passage,),
    )


async def read_image(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    path = context.policy.readable_binary_file(_path(arguments))
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_types.get(path.suffix.casefold())
    if media_type is None:
        return ToolOutcome.failure(
            "unsupported image type", code="unsupported_image_type"
        )
    content = await asyncio.to_thread(path.read_bytes)
    if len(content) > context.policy.max_file_bytes:
        return ToolOutcome.failure(
            "image exceeds the workspace read limit", code="image_too_large"
        )
    encoded = base64.b64encode(content).decode("ascii")
    relative = str(path.relative_to(context.policy.root))
    return ToolOutcome.success(
        {
            "path": relative,
            "media_type": media_type,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        content=(ToolContent.image(f"data:{media_type};base64,{encoded}", media_type),),
        audit_data={
            "path": relative,
            "media_type": media_type,
            "size_bytes": len(content),
        },
    )


async def read_tool_artifact(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.artifacts is None:
        raise ValueError("tool artifact storage is unavailable")
    artifact_id = arguments.get("artifact_id")
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 16_384)
    if (
        not isinstance(artifact_id, str)
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or not isinstance(limit, int)
        or isinstance(limit, bool)
    ):
        raise ValueError("invalid artifact read request")
    artifact, content, has_more = await context.artifacts.read(
        artifact_id,
        session_id=context.session_id,
        offset=offset,
        limit=limit,
    )
    return _outcome(
        True,
        {
            "artifact_id": artifact.id,
            "offset": offset,
            "bytes": len(content),
            "content": content.decode("utf-8", errors="replace"),
            "next_offset": offset + len(content) if has_more else None,
            "has_more": has_more,
            "sha256": artifact.sha256,
        },
        audit_data={
            "artifact_id": artifact.id,
            "offset": offset,
            "bytes": len(content),
        },
    )
