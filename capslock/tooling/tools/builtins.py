"""Composition of built-in direct-capability ToolDefinitions."""

from __future__ import annotations

from dataclasses import replace

from ..contracts import ToolMiddleware
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


def workspace_tools(
    *,
    include_collaboration: bool = True,
    include_shell: bool = True,
    include_worktree: bool = True,
    schema_budget_tokens: int = 8_000,
    middleware: tuple[ToolMiddleware, ...] = (),
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
    local_reads = {
        "search_tools",
        "list_files",
        "glob_files",
        "read_file",
        "read_image",
        "read_tool_artifact",
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
    )


__all__ = ["workspace_tools"]
