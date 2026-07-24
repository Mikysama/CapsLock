"""Safe, compact presentation metadata for model tool events."""

from __future__ import annotations

from typing import Any

from ..security import redact


_CATEGORIES = {
    "list_files": "read",
    "glob_files": "search",
    "read_file": "read",
    "read_image": "read",
    "read_skill_resource": "read",
    "get_memory": "read",
    "search_files": "search",
    "search_memories": "search",
    "git_status": "search",
    "git_diff": "search",
    "list_external_sources": "search",
    "load_skill": "read",
    "edit_file": "edit",
    "create_file": "edit",
    "write_file": "edit",
    "ask_user": "input",
    "create_task": "task",
    "list_tasks": "task",
    "get_task": "task",
    "update_task": "task",
    "shell": "command",
    "process_output": "command",
    "process_stop": "command",
    "web_search": "web",
    "web_fetch": "web",
    "search_tools": "search",
    "delegate_agents": "agent",
    "get_agent_task": "agent",
    "stop_agent_task": "agent",
    "read_pdf": "read",
    "read_notebook": "read",
    "edit_notebook": "edit",
    "create_worktree": "workspace",
    "exit_worktree": "workspace",
    "list_mcp_resources": "mcp",
    "read_mcp_resource": "mcp",
}

_TITLES = {
    "list_files": "List files",
    "glob_files": "Find files",
    "read_file": "Read file",
    "read_image": "Read image",
    "read_skill_resource": "Read Skill resource",
    "get_memory": "Read memory",
    "search_files": "Search files",
    "search_memories": "Search memories",
    "git_status": "Inspect Git status",
    "git_diff": "Inspect Git diff",
    "list_external_sources": "List external sources",
    "load_skill": "Load Skill",
    "create_task": "Create task",
    "list_tasks": "List tasks",
    "get_task": "Read task",
    "update_task": "Update task",
    "edit_file": "Edit file",
    "create_file": "Create file",
    "write_file": "Write file",
    "ask_user": "Ask user",
    "shell": "Run sandboxed command",
    "process_output": "Read process output",
    "process_stop": "Stop process",
    "web_search": "Search the Web",
    "web_fetch": "Fetch Web resource",
    "search_tools": "Search tools",
    "delegate_agents": "Delegate child Agents",
    "get_agent_task": "Read child Agent task",
    "stop_agent_task": "Stop child Agent task",
    "read_pdf": "Read PDF",
    "read_notebook": "Read Notebook",
    "edit_notebook": "Edit Notebook",
    "create_worktree": "Create worktree",
    "exit_worktree": "Exit worktree",
    "list_mcp_resources": "List MCP resources",
    "read_mcp_resource": "Read MCP resource",
}


def tool_presentation(
    name: str,
    arguments: dict[str, Any],
    *,
    outcome: str | None = None,
) -> dict[str, object]:
    """Build allowlisted event metadata without copying arbitrary arguments."""

    category = (
        "mcp"
        if name.startswith("mcp__")
        else "plugin"
        if name.startswith("plugin__")
        else _CATEGORIES.get(name, "other")
    )
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
        "web_fetch": "url",
        "process_output": "process_id",
        "process_stop": "process_id",
    }.get(name, "path")
    value = arguments.get(key)
    return str(redact(value)) if isinstance(value, str) else None


def _detail(name: str, arguments: dict[str, Any]) -> str | None:
    if name in {"search_files", "search_memories", "search_tools", "web_search"}:
        value = arguments.get("query")
    elif name == "shell":
        value = arguments.get("command")
    elif name.startswith("mcp__"):
        value = name
    elif name == "delegate_agents":
        tasks = arguments.get("tasks")
        value = f"{len(tasks)} task(s)" if isinstance(tasks, list) else None
    elif name == "update_task":
        task, status = arguments.get("task_id"), arguments.get("status")
        value = (
            f"{task}: {status}"
            if isinstance(task, str) and isinstance(status, str)
            else None
        )
    else:
        value = None
    return str(redact(value)) if isinstance(value, str) else None


def _short(value: str, limit: int) -> str:
    clean = " ".join(value.split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _humanize(name: str) -> str:
    words = [item for item in name.split("_") if item]
    if words and words[0].casefold() == "mcp":
        words[0] = "MCP"
        return " ".join(words)
    return " ".join(words).capitalize() or "Tool"
