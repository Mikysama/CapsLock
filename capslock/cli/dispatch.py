"""Async v2 slash-command routing."""

from __future__ import annotations

import shlex

from ..domain import WorkItemStatus
from . import actions
from .commands import COMMANDS, resolve_command
from .context import CliContext
from .memory import memory_command
from .skills import skills_command
from .views.workflow import StatusView, render_queue, render_status


async def dispatch_slash_command(context: CliContext, text: str) -> str:
    spec = resolve_command(text)
    if spec is None:
        context.console.print("[warning]Unknown command. Use /help.[/]")
        return "handled"
    parts = shlex.split(text)
    name = spec.path
    if name in {"/exit", "/quit"}:
        return "exit"
    if name == "/help":
        for item in COMMANDS:
            context.console.print(f"[command]{item.path:<14}[/] {item.description}")
    elif name == "/status":
        await _status(context)
    elif name == "/permissions":
        await actions.permissions(context, text)
    elif name == "/approvals":
        if len(parts) == 3 and parts[1] == "approve":
            await actions.approve_action(context, parts[2])
        elif len(parts) == 3 and parts[1] == "reject":
            await actions.reject_action(context, parts[2])
        else:
            await actions.render_approvals(context)
            context.console.print(
                "[text.secondary]Use /approvals approve <id> or /approvals reject <id>.[/]"
            )
    elif name == "/queue":
        await _queue(context, parts)
    elif name == "/memory":
        await memory_command(context, text)
    elif name == "/skills":
        await skills_command(context, text)
    elif name == "/sources":
        await actions.render_sources(context)
    elif name == "/mcp":
        await actions.mcp_command(context, text)
    elif name == "/diff":
        await actions.show_git_diff(context)
    elif name == "/undo":
        await actions.undo(context)
    elif name == "/rename":
        if len(parts) < 2:
            context.console.print("[error]Usage:[/] /rename <title>")
        else:
            session = await context.agent.repositories.sessions.rename(
                context.agent.session_id, " ".join(parts[1:])
            )
            context.console.print(f"[success]Renamed:[/] {session.title}")
    return "handled"


async def _status(context: CliContext) -> None:
    agent = context.agent
    session = await agent.repositories.sessions.require(agent.session_id)
    tasks = await agent.repositories.tasks.list(agent.session_id)
    work = await agent.repositories.workflow.list_work_items(
        agent.session_id, active_only=True
    )
    cost = await agent.repositories.workflow.session_cost(agent.session_id)
    count = await agent.repositories.sessions.message_count(agent.session_id)
    render_status(
        context.console,
        StatusView(
            session,
            str(agent.workspace),
            agent.model,
            agent.permission_mode.value,
            tasks,
            work,
            *cost,
            count,
            agent.max_context_messages,
        ),
    )


async def _queue(context: CliContext, parts: list[str]) -> None:
    repository = context.agent.repositories.workflow
    if len(parts) == 1:
        render_queue(
            context.console,
            await repository.list_work_items(
                context.agent.session_id, active_only=True
            ),
        )
        return
    if len(parts) == 3 and parts[1] == "cancel":
        item = await repository.require_work_item(parts[2])
        if item.session_id != context.agent.session_id:
            raise ValueError("work item does not belong to this session")
        await repository.update_work_item(
            item.id, WorkItemStatus.CANCELLED, error="cancelled before start"
        )
        return
    if len(parts) == 4 and parts[1] == "move":
        item = await repository.require_work_item(parts[2])
        if item.session_id != context.agent.session_id:
            raise ValueError("work item does not belong to this session")
        await repository.reorder(item.id, int(parts[3]))
        return
    if len(parts) == 3 and parts[1] == "retry":
        context.console.print(
            "[text.secondary]Retry is queued by the active TUI worker.[/]"
        )
        return
    raise ValueError("usage: /queue [cancel <id>|move <id> <position>|retry <run-id>]")
