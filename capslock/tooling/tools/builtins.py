"""Composition of built-in direct-capability ToolDefinitions."""

from __future__ import annotations

from ..contracts import ToolMiddleware
from ..executor import ToolRuntime
from .collaboration import agent_control_tools, delegation_tool
from .documents import document_tools
from .filesystem import filesystem_tools
from .git import git_tools
from .interaction import interaction_tools
from .memory import memory_tools
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
    ]
    if include_shell:
        tools.extend(shell_tools())
    if include_worktree:
        tools.extend(worktree_tools())
    if include_collaboration:
        tools[0:0] = [delegation_tool(), *agent_control_tools()]
    return ToolRuntime(
        tools,
        schema_budget_tokens=schema_budget_tokens,
        middleware=middleware,
    )


__all__ = ["workspace_tools"]
