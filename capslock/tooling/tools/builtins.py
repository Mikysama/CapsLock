"""Composition of built-in direct-capability ToolDefinitions."""

from __future__ import annotations

from dataclasses import replace

from ..contracts import ToolMiddleware
from ..contracts import ToolSelectionMode
from ..executor import ToolRuntime
from .collaboration import agent_control_tools, delegation_tool
from .documents import document_tools
from .filesystem.registry import filesystem_tools
from .git import git_tools
from .interaction import interaction_tools
from .memory import memory_tools
from .plans import plan_tools
from .shell import shell_tools
from .skills import skill_tools
from .sources import source_tools
from .tasks import task_tools
from .web import web_tools
from .worktrees import worktree_tools


_ALIASES = {
    "list_files": ("browse files", "directory listing"),
    "glob_files": ("find paths", "filename pattern"),
    "search_files": ("search text", "grep workspace"),
    "read_file": ("open file", "inspect file"),
    "create_file": ("new file",),
    "edit_file": ("replace text", "focused edit"),
    "write_file": ("replace whole file", "full rewrite"),
    "list_tasks": ("task overview",),
    "get_task": ("task details",),
    "search_memories": ("find memory",),
    "get_memory": ("memory details",),
}


def _group(name: str) -> str:
    if (
        name.endswith("_file")
        or name.endswith("_files")
        or name
        in {
            "read_image",
            "read_tool_artifact",
            "search_session_history",
        }
    ):
        return "filesystem"
    if "task" in name:
        return "tasks"
    if "memory" in name or "memories" in name:
        return "memory"
    if name.startswith("git_"):
        return "git"
    return name.split("_", 1)[0]


def workspace_tools(
    *,
    include_collaboration: bool = True,
    include_shell: bool = True,
    include_worktree: bool = True,
    schema_budget_tokens: int = 8_000,
    middleware: tuple[ToolMiddleware, ...] = (),
    selection_mode: ToolSelectionMode | str = ToolSelectionMode.SHADOW,
) -> ToolRuntime:
    tools = [
        *filesystem_tools(),
        *document_tools(),
        *git_tools(),
        *task_tools(),
        *source_tools(),
        *memory_tools(),
        *skill_tools(),
        *interaction_tools(),
        *web_tools(),
        *plan_tools(),
    ]
    if include_shell:
        tools.extend(shell_tools())
    if include_worktree:
        tools.extend(worktree_tools())
    if include_collaboration:
        tools[0:0] = [delegation_tool(), *agent_control_tools()]
    generic_output_schema: dict[str, object] = {
        "type": ["object", "array"],
    }
    tools = [
        replace(
            tool,
            contract=replace(
                tool.contract,
                output_schema=tool.contract.output_schema or generic_output_schema,
                aliases=_ALIASES.get(tool.name, ()),
                intent_tags=tuple(tool.name.split("_")),
                tool_group=_group(tool.name),
            ),
        )
        for tool in tools
    ]
    local_reads = {
        "search_tools",
        "list_files",
        "glob_files",
        "read_file",
        "read_image",
        "read_tool_artifact",
        "search_session_history",
        "search_files",
        "git_status",
        "git_diff",
        "read_pdf",
        "read_notebook",
        "search_memories",
        "get_memory",
        "load_skill",
        "read_skill_resource",
        "list_tasks",
        "get_task",
        "list_external_sources",
    }
    from ..contracts import PlanToolVisibility

    tools = [
        replace(
            tool,
            contract=replace(
                tool.contract,
                plan_visibility=PlanToolVisibility.LOCAL_READ,
            ),
        )
        if tool.name in local_reads
        else tool
        for tool in tools
    ]
    return ToolRuntime(
        tools,
        schema_budget_tokens=schema_budget_tokens,
        middleware=middleware,
        selection_mode=selection_mode,
    )


__all__ = ["workspace_tools"]
