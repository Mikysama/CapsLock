"""Safe, compact presentation metadata for model tool events."""

from __future__ import annotations

from typing import Any

from ..security import redact


_CATEGORIES = {
    "list_files": "read",
    "read_file": "read",
    "read_skill_resource": "read",
    "get_memory": "read",
    "search_files": "search",
    "search_memories": "search",
    "git_status": "search",
    "git_diff": "search",
    "list_external_sources": "search",
    "load_skill": "read",
    "propose_file_edit": "edit",
    "propose_file_create": "edit",
    "propose_command": "command",
    "propose_web_search": "web",
    "propose_web_fetch": "web",
    "propose_mcp_connect": "mcp",
    "propose_mcp_call": "mcp",
    "delegate_agents": "agent",
}

_TITLES = {
    "list_files": "List files",
    "read_file": "Read file",
    "read_skill_resource": "Read Skill resource",
    "get_memory": "Read memory",
    "search_files": "Search files",
    "search_memories": "Search memories",
    "git_status": "Inspect Git status",
    "git_diff": "Inspect Git diff",
    "list_external_sources": "List external sources",
    "load_skill": "Load Skill",
    "task_list_update": "Update task list",
    "task_status_update": "Update task status",
    "propose_file_edit": "Propose file edit",
    "propose_file_create": "Propose file creation",
    "propose_command": "Propose command",
    "propose_web_search": "Propose Web search",
    "propose_web_fetch": "Propose Web fetch",
    "propose_mcp_connect": "Propose MCP connection",
    "propose_mcp_call": "Propose MCP call",
    "delegate_agents": "Delegate child Agents",
}


def tool_presentation(
    name: str,
    arguments: dict[str, Any],
    *,
    outcome: str | None = None,
) -> dict[str, object]:
    """Build allowlisted event metadata without copying arbitrary arguments."""

    category = _CATEGORIES.get(name, "other")
    value: dict[str, object] = {
        "version": 1,
        "category": category,
        "title": _TITLES.get(name, _humanize(name)),
    }
    target = _target(name, arguments)
    detail = _detail(name, arguments)
    if target:
        value["target"] = _short(target, 240)
    if detail:
        value["detail"] = _short(detail, 240)
    if outcome:
        value["outcome"] = outcome
    return value


def _target(name: str, arguments: dict[str, Any]) -> str | None:
    key = {
        "get_memory": "memory_id",
        "load_skill": "name",
        "propose_web_fetch": "url",
        "propose_mcp_connect": "server",
    }.get(name, "path")
    value = arguments.get(key)
    return str(redact(value)) if isinstance(value, str) else None


def _detail(name: str, arguments: dict[str, Any]) -> str | None:
    if name in {"search_files", "search_memories", "propose_web_search"}:
        value = arguments.get("query")
    elif name == "propose_command":
        value = arguments.get("template")
    elif name == "propose_mcp_call":
        server, tool = arguments.get("server"), arguments.get("tool")
        value = f"{server}/{tool}" if isinstance(server, str) and isinstance(tool, str) else None
    elif name == "delegate_agents":
        tasks = arguments.get("tasks")
        value = f"{len(tasks)} task(s)" if isinstance(tasks, list) else None
    elif name == "task_status_update":
        task, status = arguments.get("task_id"), arguments.get("status")
        value = f"{task}: {status}" if isinstance(task, str) and isinstance(status, str) else None
    else:
        value = None
    return str(redact(value)) if isinstance(value, str) else None


def _short(value: str, limit: int) -> str:
    clean = " ".join(value.split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize() or "Tool"
