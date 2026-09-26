"""Focused filesystem tool handlers."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from ....evidence import Evidence
from ...contracts import ExecutionContext, ToolContent, ToolOutcome
from ..support import _outcome, _path
from .ripgrep import RipgrepProcess


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
    active_plan = bool(
        context.planning is not None
        and await context.planning.is_active(context.session_id)
    )
    names = context.catalog.search(query, limit, plan_visible_only=active_plan)
    if context.discoveries is not None:
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
    requested, query = _path(arguments), arguments.get("query")
    if not isinstance(query, str) or not query.strip() or "\x00" in query:
        raise ValueError("query must be a non-empty string")
    pattern = arguments.get("glob", arguments.get("pattern", "*"))
    window = arguments.get("context", 2)
    limit = arguments.get("limit", 8)
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        raise ValueError("pattern must be a non-empty string")
    if not isinstance(window, int) or isinstance(window, bool) or not 0 <= window <= 20:
        raise ValueError("context must be between 0 and 20")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    root = context.policy.resolve(requested)
    context.policy.readable_directory(
        requested
    ) if root.is_dir() else context.policy.readable_binary_file(requested)
    mode = arguments.get("mode", "regex")
    case_sensitive = arguments.get("case_sensitive", True)
    include_hidden = arguments.get("include_hidden", False)
    if mode not in {"regex", "literal"}:
        raise ValueError("mode must be regex or literal")
    if not isinstance(case_sensitive, bool) or not isinstance(include_hidden, bool):
        raise ValueError("case_sensitive and include_hidden must be booleans")
    executable = shutil.which("rg")
    if executable is None:
        return ToolOutcome.failure(
            "ripgrep is required for search_files; install ripgrep and run capslock doctor",
            code="search_backend_unavailable",
        )
    argv = [
        executable,
        "--json",
        "--line-number",
        "--color",
        "never",
        "--max-filesize",
        str(context.policy.max_file_bytes),
        "--case-sensitive" if case_sensitive else "--ignore-case",
    ]
    if mode == "literal":
        argv.append("--fixed-strings")
    if include_hidden:
        argv.append("--hidden")
    if pattern != "*":
        argv.extend(("--glob", pattern))
    argv.extend(("--", query, str(root)))
    try:
        backend = await RipgrepProcess.start(
            argv,
            cwd=context.policy.root,
            stream_limit=max(65_536, context.policy.max_file_bytes * 2),
        )
    except OSError:
        return ToolOutcome.failure(
            "ripgrep could not start", code="search_backend_unavailable"
        )
    passages: list[Evidence] = []
    seen_paths: set[Path] = set()
    cached_lines: dict[Path, list[str]] = {}

    def read_lines(path: Path) -> tuple[Path, list[str]]:
        # Binary validation checks the same privacy/path/size rules without an
        # additional full decode; the single read below enforces UTF-8.
        path = context.policy.readable_binary_file(str(path))
        if path not in cached_lines:
            cached_lines[path] = path.read_text(encoding="utf-8").splitlines()
        return path, cached_lines[path]

    records_seen = 0
    record_budget = max(context.policy.max_files, limit * 10)
    stop_reason = None
    async with backend:
        while raw := await backend.record():
            records_seen += 1
            if records_seen > record_budget:
                stop_reason = "scan_limit"
                break
            try:
                item = json.loads(raw)
                if item.get("type") != "match":
                    continue
                data = item["data"]
                path = Path(data["path"]["text"])
                if (
                    path not in seen_paths
                    and len(seen_paths) >= context.policy.max_files
                ):
                    stop_reason = "file_limit"
                    break
                seen_paths.add(path)
                path, lines = await asyncio.to_thread(read_lines, path)
                line_number = int(data["line_number"])
                start, end = (
                    max(1, line_number - window),
                    min(len(lines), line_number + window),
                )
                passages.append(
                    Evidence(path, start, end, "\n".join(lines[start - 1 : end]))
                )
                if len(passages) >= limit:
                    stop_reason = "result_limit"
                    break
            except (KeyError, TypeError, ValueError, UnicodeError, OSError):
                continue
        if stop_reason is None:
            await backend.process.wait()
    if stop_reason is None and backend.process.returncode not in {0, 1}:
        return backend.failure()
    metadata = {
        "backend": "ripgrep",
        "truncated": stop_reason is not None,
        "stop_reason": stop_reason,
        "path": requested,
    }
    return _outcome(
        True,
        [item.as_dict() for item in passages],
        citations=tuple(passages),
        event_data=metadata,
        content=(ToolContent.text(json.dumps(metadata)),),
    )


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

    executable = shutil.which("rg")
    if executable is None:
        return ToolOutcome.failure(
            "ripgrep is required for glob_files; install ripgrep and run capslock doctor",
            code="search_backend_unavailable",
        )
    command = [executable, "--files", "--null"]
    # A positive glob overrides ripgrep's hidden/ignore filtering. Treat the
    # default '*' as no filter, matching search_files; retain rg semantics for
    # explicit patterns and enforce include_hidden below in either case.
    if pattern != "*":
        command.extend(("--glob", pattern))
    if include_hidden:
        command.append("--hidden")
    command.extend(("--", str(root)))
    try:
        backend = await RipgrepProcess.start(command, cwd=context.policy.root)
    except OSError:
        return ToolOutcome.failure(
            "ripgrep could not start", code="search_backend_unavailable"
        )
    files: set[str] = set()
    stop_reason = None
    records_seen = 0
    async with backend:
        while raw := await backend.record(b"\0"):
            records_seen += 1
            if records_seen > context.policy.max_files:
                stop_reason = "scan_limit"
                break
            try:
                item = context.policy.resolve(os.fsdecode(raw.removesuffix(b"\0")))
                relative = item.relative_to(root)
                if (
                    not item.is_file()
                    or not context.policy.is_agent_readable(item)
                    or (
                        not include_hidden
                        and any(part.startswith(".") for part in relative.parts)
                    )
                ):
                    continue
                value = str(item.relative_to(context.policy.root))
                if value in files:
                    continue
                if len(files) == limit:
                    stop_reason = "result_limit"
                    break
                files.add(value)
            except (OSError, ValueError):
                continue
        if stop_reason is None:
            await backend.process.wait()
    if stop_reason is None and backend.process.returncode not in {0, 1}:
        return backend.failure()
    return ToolOutcome.success(
        {
            "pattern": pattern,
            "path": requested,
            "files": sorted(files),
            "count": len(files),
            "truncated": stop_reason is not None,
            "backend": "ripgrep",
            "stop_reason": stop_reason,
        }
    )
