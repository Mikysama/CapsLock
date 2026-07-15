"""Session task and external-source tools."""

from __future__ import annotations

from typing import Any

from .core import RunContext, ToolResult


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


def list_external_sources(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    if context.store is None:
        raise ValueError("source storage is unavailable")
    sources = context.store.list_sources(context.session_id)
    return ToolResult(True, [{"source_id": item.id, "url": item.url, "title": item.title, "excerpt": item.excerpt, "fetched_at": item.fetched_at, "untrusted": True, "suspicious": item.suspicious} for item in sources], source_ids=tuple(item.id for item in sources))
