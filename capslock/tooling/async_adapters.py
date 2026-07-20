"""Async tool adapters for workspace, actions, tasks, memory, and Skills."""

from __future__ import annotations

import asyncio
import fnmatch
from pathlib import Path
from typing import Any

from ..domain import ActionRecord, ActionType
from ..evidence import Evidence
from ..security import TEXT_SUFFIXES
from .async_core import RunContext, ToolResult


def _path(arguments: dict[str, Any]) -> str:
    path = arguments.get("path", ".")
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    return path


async def list_files(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
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
    return ToolResult(
        True, {"path": _path(arguments), "files": files, "count": len(files)}
    )


async def read_file(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    def read() -> tuple[Path, Evidence]:
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
        return path, Evidence(path, start, end, "\n".join(lines[start - 1 : end]))

    path, passage = await asyncio.to_thread(read)
    return ToolResult(
        True,
        {
            "path": str(path),
            "start_line": passage.start_line,
            "end_line": passage.end_line,
            "text": passage.text,
            "evidence_id": passage.id,
        },
        citations=(passage,),
    )


async def search_files(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    def search() -> list[Evidence]:
        requested, query = _path(arguments), arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        pattern = arguments.get("pattern", "*")
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
                    start, end = max(0, index - 2), min(len(lines), index + 3)
                    output.append(
                        Evidence(item, start + 1, end, "\n".join(lines[start:end]))
                    )
                    if len(output) >= 8:
                        return output
        return output

    passages = await asyncio.to_thread(search)
    return ToolResult(
        True, [item.as_dict() for item in passages], citations=tuple(passages)
    )


async def _git(context: RunContext, *args: str) -> ToolResult:
    if not (context.policy.root / ".git").exists():
        return ToolResult(False, {}, "workspace is not a Git repository")
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
        return ToolResult(False, {}, "git command timed out")
    if process.returncode:
        return ToolResult(
            False, {}, stderr.decode("utf-8", "replace").strip() or "git command failed"
        )
    return ToolResult(True, {"output": stdout[:100000].decode("utf-8", "replace")})


async def git_status(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    return await _git(context, "status", "--short")


async def git_diff(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    path = arguments.get("path")
    if path is not None:
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        context.policy.resolve(path)
        return await _git(context, "diff", "--", path)
    return await _git(context, "diff")


def action_data(action: ActionRecord) -> dict[str, object]:
    return {
        "action_id": action.id,
        "kind": action.type.value,
        "summary": action.summary,
        "status": action.status.value,
        "result_kind": action.result_kind.value if action.result_kind else None,
        "request": action.request,
        "result": action.result,
        "error": action.error_message,
    }


async def propose_action(
    context: RunContext, action_type: ActionType, arguments: dict[str, Any]
) -> ToolResult:
    return ToolResult(
        True, action_data(await context.actions.propose(action_type, **arguments))
    )


async def propose_web_search(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
    return await propose_action(context, ActionType.WEB_SEARCH, arguments)


async def propose_web_fetch(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
    return await propose_action(context, ActionType.WEB_FETCH, arguments)


async def propose_mcp_connect(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
    return await propose_action(context, ActionType.MCP_CONNECT, arguments)


async def propose_mcp_call(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
    return await propose_action(context, ActionType.MCP_CALL, arguments)


async def propose_command(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    return await propose_action(context, ActionType.COMMAND, arguments)


async def propose_file_edit(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
    return await propose_action(context, ActionType.FILE_EDIT, arguments)


async def propose_file_create(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
    return await propose_action(context, ActionType.FILE_CREATE, arguments)


async def task_list_update(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
    items = arguments.get("items")
    if not isinstance(items, list) or not all(
        isinstance(item, str) and item.strip() for item in items
    ):
        raise ValueError("items must be a list of non-empty task strings")
    tasks = await context.repositories.tasks.replace(
        context.session_id, items, run_id=context.run_id
    )
    return ToolResult(
        True,
        {
            "tasks": [
                {"id": item.id, "text": item.text, "status": item.status}
                for item in tasks
            ]
        },
    )


async def task_status_update(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
    task_id, status = arguments.get("task_id"), arguments.get("status")
    if not isinstance(task_id, str) or not isinstance(status, str):
        raise ValueError("task_id and status must be strings")
    item = await context.repositories.tasks.update_status(
        task_id, context.session_id, status
    )
    return ToolResult(True, {"id": item.id, "text": item.text, "status": item.status})


async def list_external_sources(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
    items = await context.repositories.sources.list(context.session_id)
    return ToolResult(
        True,
        [
            {
                "source_id": item.id,
                "url": item.url,
                "title": item.title,
                "excerpt": item.excerpt,
                "fetched_at": item.fetched_at,
                "untrusted": True,
                "suspicious": item.suspicious,
            }
            for item in items
        ],
        source_ids=tuple(item.id for item in items),
    )


async def search_memories(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    if context.memory is None:
        raise ValueError("memory storage is unavailable")
    query, limit = arguments.get("query"), arguments.get("limit", 10)
    if not isinstance(query, str) or not isinstance(limit, int):
        raise ValueError("query must be a string and limit must be an integer")
    items = await context.memory.search(query, run_id=context.run_id, limit=limit)
    return ToolResult(
        True,
        [_memory_data(item) for item in items],
        memories=tuple(items),
        audit_data={"memory_ids": [item.id for item in items]},
        audit_arguments={},
    )


async def get_memory(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    if context.memory is None:
        raise ValueError("memory storage is unavailable")
    identifier = arguments.get("memory_id")
    if not isinstance(identifier, str):
        raise ValueError("memory_id must be a string")
    item = await context.memory.get_for_model(identifier, run_id=context.run_id)
    return ToolResult(
        True,
        _memory_data(item),
        memories=(item,),
        audit_data={"memory_id": item.id},
        audit_arguments={"memory_id": item.id},
    )


def _memory_data(item: Any) -> dict[str, object]:
    return {
        "memory_id": item.id,
        "content": item.content,
        "type": item.type.value,
        "scope": item.scope.value,
        "source": {"kind": item.source_kind, "ref": item.source_ref},
        "confidence": item.confidence,
        "expires_at": item.expires_at,
        "revision": item.revision,
        "citation": f"[[memory:{item.id}]]",
    }


async def load_skill(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    if not isinstance(name, str) or context.skills is None:
        raise ValueError("Skill name or loader is unavailable")
    data, audit = await asyncio.to_thread(
        context.skills.load_data, context.run_id, name, trigger="model"
    )
    return ToolResult(True, data, audit_data=audit)


async def read_skill_resource(
    context: RunContext, arguments: dict[str, Any]
) -> ToolResult:
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
    return ToolResult(True, data, audit_data=audit)
