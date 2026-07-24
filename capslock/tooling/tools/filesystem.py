"""Filesystem direct-capability tool execution."""

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


async def search_tools(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.catalog is None:
        return ToolOutcome.failure(
            "tool catalog is unavailable", code="catalog_unavailable"
        )
    query = arguments.get("query")
    limit = arguments.get("limit", 5)
    if not isinstance(query, str) or not query.strip():
        return ToolOutcome.failure(
            "query must be a non-empty string", code="invalid_query"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        return ToolOutcome.failure(
            "limit must be between 1 and 20", code="invalid_limit"
        )
    names = context.catalog.search(query, limit)
    if context.discoveries is not None and hasattr(
        context.discoveries, "record_tool_discoveries"
    ):
        await context.discoveries.record_tool_discoveries(
            context.session_id, list(names), context.catalog.snapshot().generation
        )
    return ToolOutcome.success(
        {
            "tools": list(names),
            "count": len(names),
            "available_next_turn": True,
        }
    )


async def search_files(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    def fallback_search() -> list[Evidence]:
        requested, query = _path(arguments), arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        pattern = arguments.get("glob", arguments.get("pattern", "*"))
        if not isinstance(pattern, str):
            raise ValueError("pattern must be a string")
        root = context.policy.resolve(requested)
        context.policy.readable_directory(
            requested
        ) if root.is_dir() else context.policy.readable_file(requested)
        candidates = (
            [root]
            if root.is_file()
            else [item for item in root.rglob("*") if item.is_file()]
        )
        terms, output = [item.casefold() for item in query.split() if item.strip()], []
        for item in candidates[: context.policy.max_files]:
            if (
                not context.policy.is_agent_readable(item)
                or item.suffix.lower() not in TEXT_SUFFIXES
                or not fnmatch.fnmatch(item.name, pattern)
                or item.stat().st_size > context.policy.max_file_bytes
            ):
                continue
            try:
                lines = item.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines):
                lower = line.casefold()
                if query.casefold() in lower or any(term in lower for term in terms):
                    window = int(arguments.get("context", 2))
                    start, end = (
                        max(0, index - window),
                        min(len(lines), index + window + 1),
                    )
                    output.append(
                        Evidence(item, start + 1, end, "\n".join(lines[start:end]))
                    )
                    if len(output) >= int(arguments.get("limit", 8)):
                        return output
        return output

    requested, query = _path(arguments), arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    root = context.policy.resolve(requested)
    context.policy.readable_directory(
        requested
    ) if root.is_dir() else context.policy.readable_file(requested)
    executable = shutil.which("rg")
    passages: list[Evidence] = []
    if executable is not None:
        pattern = arguments.get("glob", arguments.get("pattern", "*"))
        window = int(arguments.get("context", 2))
        limit = int(arguments.get("limit", 8))
        argv = [executable, "--json", "--line-number", "--color", "never"]
        if isinstance(pattern, str) and pattern != "*":
            argv.extend(("--glob", pattern))
        argv.extend(("--", query, str(root)))
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        if process.returncode in {0, 1}:
            for raw in stdout.splitlines():
                try:
                    item = json.loads(raw)
                    if item.get("type") != "match":
                        continue
                    data = item["data"]
                    path = Path(data["path"]["text"]).resolve()
                    if not context.policy.is_agent_readable(path):
                        continue
                    line_number = int(data["line_number"])
                    lines = path.read_text(encoding="utf-8").splitlines()
                    start = max(1, line_number - window)
                    end = min(len(lines), line_number + window)
                    passages.append(
                        Evidence(path, start, end, "\n".join(lines[start - 1 : end]))
                    )
                    if len(passages) >= limit:
                        break
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    UnicodeError,
                    json.JSONDecodeError,
                ):
                    continue
    else:
        passages = await asyncio.to_thread(fallback_search)
    return _outcome(
        True, [item.as_dict() for item in passages], citations=tuple(passages)
    )


async def edit_file(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    return await execute_action_tool(context, ActionType.FILE_EDIT, arguments)


async def create_file(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
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


async def glob_files(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    pattern = arguments.get("pattern")
    requested = arguments.get("path", ".")
    limit = int(arguments.get("limit", 100))
    include_hidden = bool(arguments.get("include_hidden", False))
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern must be a non-empty string")
    if not isinstance(requested, str):
        raise ValueError("path must be a string")
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    root = context.policy.readable_directory(requested)

    async def ripgrep() -> list[str] | None:
        executable = shutil.which("rg")
        if executable is None:
            return None
        command = [executable, "--files", "--glob", pattern]
        if include_hidden:
            command.append("--hidden")
        command.append(str(root))
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=context.policy.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode not in {0, 1}:
            return None
        values: list[str] = []
        for raw in stdout.decode("utf-8", "replace").splitlines():
            item = Path(raw).resolve()
            if item.is_file() and context.policy.is_agent_readable(item):
                values.append(str(item.relative_to(context.policy.root)))
        return sorted(set(values))

    files = await ripgrep()
    if files is None:

        def fallback() -> list[str]:
            values = []
            for item in root.rglob("*"):
                relative = item.relative_to(root)
                if (
                    item.is_file()
                    and (
                        include_hidden
                        or not any(part.startswith(".") for part in relative.parts)
                    )
                    and not _gitignored(root, relative)
                    and _glob_match(relative, pattern)
                    and context.policy.is_agent_readable(item)
                ):
                    values.append(str(item.relative_to(context.policy.root)))
            return sorted(values)

        files = await asyncio.to_thread(fallback)
    truncated = len(files) > limit
    return ToolOutcome.success(
        {
            "pattern": pattern,
            "path": requested,
            "files": files[:limit],
            "count": min(len(files), limit),
            "truncated": truncated,
        }
    )


def _gitignored(root: Path, relative: Path) -> bool:
    """Apply the common .gitignore subset used by the pure-Python fallback."""
    ignored = False
    candidates: list[tuple[Path, Path]] = []
    parents = [root]
    current = root
    for part in relative.parts[:-1]:
        current /= part
        parents.append(current)
    for parent in parents:
        ignore_file = parent / ".gitignore"
        if ignore_file.is_file():
            try:
                base = parent.relative_to(root)
                local = relative.relative_to(base) if base.parts else relative
                candidates.append((ignore_file, local))
            except (OSError, UnicodeError, ValueError):
                continue
    for ignore_file, local in candidates:
        try:
            lines = ignore_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        value = local.as_posix()
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            rule = line[1:] if negate else line
            directory = rule.endswith("/")
            rule = rule.rstrip("/")
            anchored = rule.startswith("/")
            rule = rule.lstrip("/")
            if not rule:
                continue
            if directory:
                matched = value == rule or value.startswith(rule + "/")
            elif anchored or "/" in rule:
                matched = local.match(rule)
            else:
                matched = any(Path(part).match(rule) for part in local.parts)
            if matched:
                ignored = not negate
    return ignored


def _glob_match(relative: Path, pattern: str) -> bool:
    if relative.match(pattern):
        return True
    while pattern.startswith("**/"):
        pattern = pattern[3:]
        if relative.match(pattern):
            return True
    return False


def filesystem_tools():
    from ..contracts import ResolvedToolPolicy, define_tool
    from .schemas import _int, _schema, _str

    safe_read = ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "search_tools",
            "Search deferred plugin, MCP, and large-schema tools. Matches become available on the next model turn.",
            _schema(
                {
                    "query": _str(),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["query"],
            ),
            search_tools,
            policy=safe_read,
        ),
        define_tool(
            "list_files",
            "List readable workspace files.",
            _schema({"path": _str(), "pattern": _str()}, ["path"]),
            list_files,
            policy=safe_read,
        ),
        define_tool(
            "glob_files",
            "Find workspace files by path glob with stable ordering and truncation metadata.",
            _schema(
                {
                    "pattern": _str(),
                    "path": _str(),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
                    "include_hidden": {"type": "boolean"},
                },
                ["pattern"],
            ),
            glob_files,
            policy=safe_read,
        ),
        define_tool(
            "read_file",
            "Read a UTF-8 workspace file with evidence.",
            _schema(
                {"path": _str(), "start_line": _int(), "end_line": _int()}, ["path"]
            ),
            read_file,
            policy=safe_read,
        ),
        define_tool(
            "read_image",
            "Read a PNG, JPEG, GIF, or WebP workspace image as a rich image result.",
            _schema({"path": _str()}, ["path"]),
            read_image,
            policy=safe_read,
        ),
        define_tool(
            "read_tool_artifact",
            "Read a session-scoped tool artifact in bounded chunks.",
            _schema(
                {
                    "artifact_id": _str(),
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 16384},
                },
                ["artifact_id"],
            ),
            read_tool_artifact,
            policy=safe_read,
            inline_result_bytes=16_384,
        ),
        define_tool(
            "search_files",
            "Search readable workspace text and return evidence.",
            _schema(
                {
                    "path": _str(),
                    "query": _str(),
                    "glob": _str(),
                    "context": {"type": "integer", "minimum": 0, "maximum": 20},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["path", "query"],
            ),
            search_files,
            policy=safe_read,
        ),
        define_tool(
            "edit_file",
            "Apply an exact text replacement through the durable approval workflow.",
            _schema(
                {
                    "path": _str(),
                    "old_text": _str(),
                    "new_text": _str(),
                    "summary": _str(),
                },
                ["path", "old_text", "new_text"],
            ),
            edit_file,
        ),
        define_tool(
            "create_file",
            "Create a text file through the durable approval workflow.",
            _schema(
                {"path": _str(), "content": _str(), "summary": _str()},
                ["path", "content"],
            ),
            create_file,
        ),
        define_tool(
            "write_file",
            "Write complete text content with a required read hash precondition through the durable Action workflow.",
            _schema(
                {
                    "path": _str(),
                    "content": _str(),
                    "expected_sha256": {"type": ["string", "null"]},
                    "summary": _str(),
                },
                ["path", "content", "expected_sha256"],
            ),
            write_file,
        ),
    ]


__all__ = [
    "search_tools",
    "list_files",
    "glob_files",
    "read_file",
    "read_image",
    "read_tool_artifact",
    "search_files",
    "edit_file",
    "create_file",
    "write_file",
    "filesystem_tools",
]
