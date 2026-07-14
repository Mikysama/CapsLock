"""Registered, read-only workspace tools for the model runtime."""

from __future__ import annotations

import fnmatch
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .changes import ChangeService
from .evidence import Evidence
from .execution import CommandService
from .policy import PolicyError, WorkspacePolicy
from .session import SessionStore


TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml", ".toml", ".csv", ".html", ".css", ".sh", ".sql", ".go", ".rs", ".java", ".c", ".h", ".cpp"}


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: object
    error: str | None = None
    citations: tuple[Evidence, ...] = ()

    def for_model(self) -> str:
        return json.dumps({"ok": self.ok, "data": self.data, "error": self.error}, ensure_ascii=False)


@dataclass(frozen=True)
class RunContext:
    session_id: str
    run_id: str
    policy: WorkspacePolicy
    max_turns: int
    event: Callable[..., None]
    store: SessionStore | None = None
    command_timeout_seconds: float = 120
    command_output_bytes: int = 100_000


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, object]
    risk_level: str
    execute: Callable[[RunContext, dict[str, Any]], ToolResult]
    requires_approval: bool = False
    reversible: bool = False

    def schema(self) -> dict[str, object]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    @property
    def schemas(self) -> list[dict[str, object]]:
        return [tool.schema() for tool in self._tools.values()]

    def invoke(self, name: str, context: RunContext, arguments: dict[str, Any]) -> tuple[ToolResult, int]:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, {}, f"unsupported tool: {name}"), 0
        started = time.monotonic()
        context.event("tool_started", name=name, risk=tool.risk_level)
        try:
            result = tool.execute(context, arguments)
        except (PolicyError, ValueError, OSError) as exc:
            result = ToolResult(False, {}, str(exc))
        duration_ms = round((time.monotonic() - started) * 1000)
        context.event("tool_finished", name=name, ok=result.ok, duration_ms=duration_ms)
        return result, duration_ms


def _path(arguments: dict[str, Any]) -> str:
    path = arguments.get("path", ".")
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    return path


def _evidence(path: Path, query: str, context_lines: int = 2, limit: int = 8) -> list[Evidence]:
    terms = [item.casefold() for item in query.split() if item.strip()]
    lines = path.read_text(encoding="utf-8").splitlines()
    found: list[Evidence] = []
    for index, line in enumerate(lines):
        lower = line.casefold()
        if query.casefold() in lower or any(term in lower for term in terms):
            start, end = max(0, index - context_lines), min(len(lines), index + context_lines + 1)
            found.append(Evidence(path, start + 1, end, "\n".join(lines[start:end])))
            if len(found) >= limit:
                break
    return found


def list_files(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    directory = context.policy.resolve(_path(arguments))
    if not directory.is_dir():
        raise ValueError(f"directory does not exist: {directory}")
    pattern = arguments.get("pattern", "*")
    if not isinstance(pattern, str):
        raise ValueError("pattern must be a string")
    files = [item for item in sorted(directory.rglob("*")) if item.is_file() and fnmatch.fnmatch(item.name, pattern)][: context.policy.max_files]
    return ToolResult(True, {"path": str(directory), "files": [str(item.relative_to(context.policy.root)) for item in files], "count": len(files)})


def read_file(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    path = context.policy.readable_file(_path(arguments))
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError(f"unsupported text file type: {path.suffix or '(none)'}")
    lines = path.read_text(encoding="utf-8").splitlines()
    start = int(arguments.get("start_line", 1))
    end = int(arguments.get("end_line", len(lines)))
    if start < 1 or end < start:
        raise ValueError("line range must satisfy 1 <= start_line <= end_line")
    end = min(end, len(lines))
    passage = Evidence(path, start, end, "\n".join(lines[start - 1:end]))
    return ToolResult(True, {"path": str(path), "start_line": start, "end_line": end, "text": passage.text, "evidence_id": passage.id}, citations=(passage,))


def search_files(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    directory = context.policy.resolve(_path(arguments))
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    pattern = arguments.get("pattern", "*")
    if not isinstance(pattern, str):
        raise ValueError("pattern must be a string")
    candidates = [directory] if directory.is_file() else [item for item in directory.rglob("*") if item.is_file()]
    output: list[Evidence] = []
    for item in candidates[: context.policy.max_files]:
        if item.suffix.lower() not in TEXT_SUFFIXES or not fnmatch.fnmatch(item.name, pattern) or item.stat().st_size > context.policy.max_file_bytes:
            continue
        try:
            output.extend(_evidence(item, query, limit=8 - len(output)))
        except UnicodeDecodeError:
            continue
        if len(output) >= 8:
            break
    return ToolResult(True, [passage.as_dict() for passage in output], citations=tuple(output))


def _git(context: RunContext, *args: str) -> ToolResult:
    if not (context.policy.root / ".git").exists():
        return ToolResult(False, {}, "workspace is not a Git repository")
    completed = subprocess.run(["git", "-C", str(context.policy.root), *args], capture_output=True, text=True, timeout=10, check=False)
    if completed.returncode:
        return ToolResult(False, {}, completed.stderr.strip() or "git command failed")
    return ToolResult(True, {"output": completed.stdout[:100_000]})


def git_status(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    return _git(context, "status", "--short")


def git_diff(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    path = arguments.get("path")
    if path is not None:
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        context.policy.resolve(path)
        return _git(context, "diff", "--", path)
    return _git(context, "diff")


def task_list_update(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    items = arguments.get("items")
    if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError("items must be a non-empty list of task strings")
    if context.store is None:
        return ToolResult(True, {"items": items, "note": "Tasks are session-scoped for this run."})
    tasks = context.store.replace_tasks(context.session_id, items)
    return ToolResult(True, {"tasks": [{"id": task.id, "text": task.text, "status": task.status} for task in tasks]})


def task_status_update(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    task_id, status = arguments.get("task_id"), arguments.get("status")
    if not isinstance(task_id, str) or not isinstance(status, str):
        raise ValueError("task_id and status must be strings")
    if context.store is None:
        raise ValueError("task storage is unavailable")
    task = context.store.update_task_status(task_id, context.session_id, status)
    return ToolResult(True, {"id": task.id, "text": task.text, "status": task.status})


def _changes(context: RunContext) -> ChangeService:
    if context.store is None:
        raise ValueError("change storage is unavailable")
    return ChangeService(context.store, context.policy, context.session_id, context.run_id, context.event)


def _change_data(change: object) -> dict[str, object]:
    from .session import ChangeInfo
    assert isinstance(change, ChangeInfo)
    return {"change_id": change.id, "path": change.path, "operation": change.operation, "summary": change.summary, "status": change.status, "diff": change.diff}


def _commands(context: RunContext) -> CommandService:
    if context.store is None:
        raise ValueError("command storage is unavailable")
    return CommandService(context.store, context.policy, context.session_id, context.run_id, context.event, timeout_seconds=context.command_timeout_seconds, output_limit_bytes=context.command_output_bytes)


def _command_data(command: object) -> dict[str, object]:
    from .session import CommandInfo
    assert isinstance(command, CommandInfo)
    return {"command_id": command.id, "template": command.template, "argv": list(command.argv), "cwd": command.cwd, "summary": command.summary, "status": command.status, "exit_code": command.exit_code, "stdout": command.stdout, "stderr": command.stderr}


def propose_command(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    template, target, cwd = arguments.get("template"), arguments.get("target"), arguments.get("cwd", ".")
    if not isinstance(template, str) or target is not None and not isinstance(target, str) or not isinstance(cwd, str):
        raise ValueError("template, target, and cwd must be strings")
    return ToolResult(True, _command_data(_commands(context).propose(template, target=target, cwd=cwd)))


def run_command(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    command_id = arguments.get("command_id")
    if not isinstance(command_id, str):
        raise ValueError("command_id must be a string")
    return ToolResult(True, _command_data(_commands(context).execute(command_id)))


def discard_command(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    command_id = arguments.get("command_id")
    if not isinstance(command_id, str):
        raise ValueError("command_id must be a string")
    return ToolResult(True, _command_data(_commands(context).reject(command_id)))


def propose_file_edit(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    path, old_text, new_text = arguments.get("path"), arguments.get("old_text"), arguments.get("new_text")
    summary = arguments.get("summary", "")
    if not all(isinstance(item, str) for item in (path, old_text, new_text, summary)):
        raise ValueError("path, old_text, new_text, and summary must be strings")
    return ToolResult(True, _change_data(_changes(context).propose_edit(path, old_text, new_text, summary)))


def propose_file_create(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    path, content, summary = arguments.get("path"), arguments.get("content"), arguments.get("summary", "")
    if not all(isinstance(item, str) for item in (path, content, summary)):
        raise ValueError("path, content, and summary must be strings")
    return ToolResult(True, _change_data(_changes(context).propose_create(path, content, summary)))


def apply_change(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    change_id = arguments.get("change_id")
    if not isinstance(change_id, str):
        raise ValueError("change_id must be a string")
    return ToolResult(True, _change_data(_changes(context).apply(change_id)))


def discard_change(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    change_id = arguments.get("change_id")
    if not isinstance(change_id, str):
        raise ValueError("change_id must be a string")
    return ToolResult(True, _change_data(_changes(context).reject(change_id)))


def workspace_tools() -> ToolRegistry:
    string_path = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}
    return ToolRegistry([
        Tool("list_files", "List files under a workspace directory. Read-only.", {**string_path, "properties": {"path": {"type": "string"}, "pattern": {"type": "string"}}}, "read", list_files),
        Tool("read_file", "Read a UTF-8 text or source file with line-addressable evidence.", {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"], "additionalProperties": False}, "read", read_file),
        Tool("search_files", "Search text and source files, returning evidence with paths and lines.", {"type": "object", "properties": {"path": {"type": "string"}, "query": {"type": "string"}, "pattern": {"type": "string"}}, "required": ["path", "query"], "additionalProperties": False}, "read", search_files),
        Tool("git_status", "Show Git working-tree status. Read-only.", {"type": "object", "properties": {}, "additionalProperties": False}, "read", git_status),
        Tool("git_diff", "Show Git diff, optionally limited to a workspace path. Read-only.", {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False}, "read", git_diff),
        Tool("task_list_update", "Update the session task list without writing user files.", {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}}, "required": ["items"], "additionalProperties": False}, "none", task_list_update),
        Tool("task_status_update", "Update one session task status after work completes, fails, or is blocked.", {"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "running", "blocked", "completed", "failed", "cancelled"]}}, "required": ["task_id", "status"], "additionalProperties": False}, "none", task_status_update),
        Tool("propose_file_edit", "Propose one exact text replacement. This never writes a file; the user must approve before application.", {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "summary": {"type": "string"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}, "write", propose_file_edit, reversible=True),
        Tool("propose_file_create", "Propose creation of one text file. This never writes a file; the user must approve before application.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "summary": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}, "write", propose_file_create, reversible=True),
        Tool("apply_change", "Apply an already user-approved change proposal. Never call this before approval.", {"type": "object", "properties": {"change_id": {"type": "string"}}, "required": ["change_id"], "additionalProperties": False}, "write", apply_change, requires_approval=True, reversible=True),
        Tool("discard_change", "Discard a pending change proposal without writing files.", {"type": "object", "properties": {"change_id": {"type": "string"}}, "required": ["change_id"], "additionalProperties": False}, "none", discard_change),
        Tool("propose_command", "Propose a fixed, approved command template. This never starts a process; the user must approve it in the CLI.", {"type": "object", "properties": {"template": {"type": "string", "enum": ["pytest", "npm_test", "npm_build", "ruff_check", "prettier_check"]}, "target": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["template"], "additionalProperties": False}, "execute", propose_command),
        Tool("run_command", "Run an already user-approved command proposal. Never call this before approval.", {"type": "object", "properties": {"command_id": {"type": "string"}}, "required": ["command_id"], "additionalProperties": False}, "execute", run_command, requires_approval=True),
        Tool("discard_command", "Discard a pending command proposal without starting a process.", {"type": "object", "properties": {"command_id": {"type": "string"}}, "required": ["command_id"], "additionalProperties": False}, "none", discard_command),
    ])
