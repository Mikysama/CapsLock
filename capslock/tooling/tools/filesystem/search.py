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
    def fallback_search() -> list[Evidence]:
        candidates = (
            (root,)
            if root.is_file()
            else (item for item in root.rglob("*") if item.is_file())
        )
        terms, output = [item.casefold() for item in query.split() if item.strip()], []
        for item in itertools.islice(candidates, context.policy.max_files):
            relative = item.relative_to(root).as_posix() if root.is_dir() else item.name
            if not (
                fnmatch.fnmatch(item.name, pattern)
                or fnmatch.fnmatch(relative, pattern)
            ):
                continue
            try:
                readable = context.policy.readable_file(str(item))
                lines = readable.read_text(encoding="utf-8").splitlines()
            except (OSError, ValueError):
                continue
            for index, line in enumerate(lines):
                lower = line.casefold()
                if query.casefold() in lower or any(term in lower for term in terms):
                    start, end = (
                        max(0, index - window),
                        min(len(lines), index + window + 1),
                    )
                    output.append(
                        Evidence(readable, start + 1, end, "\n".join(lines[start:end]))
                    )
                    if len(output) >= limit:
                        return output
        return output

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
    ) if root.is_dir() else context.policy.readable_file(requested)
    executable = shutil.which("rg")
    passages: list[Evidence] = []
    if executable is not None:
        argv = [
            executable,
            "--json",
            "--line-number",
            "--color",
            "never",
            "--max-filesize",
            str(context.policy.max_file_bytes),
        ]
        if pattern != "*":
            argv.extend(("--glob", pattern))
        argv.extend(("--", query, str(root)))
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=max(65_536, context.policy.max_file_bytes * 2),
        )
        assert process.stdout is not None
        seen_paths: set[Path] = set()
        records_seen = 0
        record_budget = max(context.policy.max_files, limit * 10)
        try:
            while raw := await process.stdout.readline():
                records_seen += 1
                if records_seen > record_budget:
                    break
                try:
                    item = json.loads(raw)
                    if item.get("type") != "match":
                        continue
                    data = item["data"]
                    path = Path(data["path"]["text"]).resolve()
                    if (
                        path not in seen_paths
                        and len(seen_paths) >= context.policy.max_files
                    ):
                        break
                    seen_paths.add(path)
                    try:
                        path = context.policy.readable_file(str(path))
                    except ValueError:
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
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                async with asyncio.timeout(1):
                    await process.wait()
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
    else:
        passages = await asyncio.to_thread(fallback_search)
    return _outcome(
        True, [item.as_dict() for item in passages], citations=tuple(passages)
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
