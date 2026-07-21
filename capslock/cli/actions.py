"""Async CLI controllers for approvals, permissions, sources, MCP, diff, and undo."""

from __future__ import annotations

import asyncio
import shlex

from ..domain import ActionRecord, ActionStatus
from ..layout import ProjectLayout
from ..mcp import McpRegistry
from ..permissions import PermissionMode
from .context import CliContext
from .views.actions import render_approvals as render_approval_view
from .views.actions import render_sources as render_source_view
from .prompt import select_permission_mode


async def render_approvals(context: CliContext) -> None:
    items = await context.agent.repositories.actions.list(
        context.agent.session_id,
        statuses={ActionStatus.PENDING, ActionStatus.APPROVED, ActionStatus.RUNNING},
    )
    render_approval_view(context.console, items)


async def render_sources(context: CliContext) -> None:
    render_source_view(
        context.console,
        await context.agent.repositories.sources.list(context.agent.session_id),
    )


async def approve_action(context: CliContext, prefix: str) -> None:
    coordinator = context.agent.action_factory("cli")
    try:
        action = await coordinator.resolve(prefix)
        context.console.print(
            f"[warning]Approve {action.type.value}:[/] {action.summary}\n{action.request}"
        )
        answer = await asyncio.to_thread(
            context.console.input, "Approve and execute? [y/N] "
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            context.console.print("[waiting]Action remains pending.[/]")
            return
        result = await coordinator.for_run(action.run_id).approve_and_execute(action.id)
        context.console.print(
            f"[success]{result.status.value}:[/] {result.id[:12]} {result.result or ''}"
        )
        await context.agent.workflow.settle_approval(
            context.agent.session_id, action.run_id
        )
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def reject_action(context: CliContext, prefix: str) -> None:
    coordinator = context.agent.action_factory("cli")
    try:
        action = await coordinator.resolve(prefix)
        await coordinator.for_run(action.run_id).reject(action.id)
        await context.agent.workflow.settle_approval(
            context.agent.session_id, action.run_id
        )
        context.console.print(f"[warning]Rejected {action.type.value}.[/]")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def apply_action_decision(
    context: CliContext, action: ActionRecord, decision: str
) -> None:
    coordinator = context.agent.action_factory("cli").for_run(action.run_id)
    if decision == "later":
        context.console.print(f"[waiting]Action remains pending:[/] {action.id[:12]}")
        return
    if decision == "reject":
        await coordinator.reject(action.id)
        await context.agent.workflow.settle_approval(
            context.agent.session_id, action.run_id
        )
        context.console.print(
            f"[warning]Rejected {action.type.value}:[/] {action.id[:12]}"
        )
        return
    if decision != "approve":
        raise ValueError(f"unsupported action decision: {decision}")
    result = await coordinator.approve_and_execute(action.id)
    context.console.print(
        f"[success]{result.status.value}:[/] {result.id[:12]} {result.result or ''}"
    )
    await context.agent.workflow.settle_approval(
        context.agent.session_id, action.run_id
    )


async def undo(context: CliContext) -> None:
    try:
        action = await context.agent.action_factory("cli").reverse_last_file_action()
        context.console.print(f"[success]Undone:[/] {action.request.get('path')}")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def set_permission_mode(context: CliContext, value: str) -> None:
    try:
        mode = PermissionMode.parse(value)
        context.agent.permission_mode = mode
        await context.agent.repositories.settings.set_workspace(
            "permission_mode", mode.value
        )
        context.console.print(f"[success]Permission mode:[/] {mode.value}")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def permissions(context: CliContext, text: str) -> None:
    parts = shlex.split(text)
    if len(parts) == 1:
        try:
            selected = await asyncio.to_thread(
                select_permission_mode, context.agent.permission_mode
            )
        except (EOFError, KeyboardInterrupt):
            context.console.print("[waiting]Permission mode unchanged.[/]")
            return
        await set_permission_mode(context, selected.value)
        return
    if len(parts) != 2:
        context.console.print("[error]Usage:[/] /permissions [full|approve|ask]")
        return
    await set_permission_mode(context, parts[1])


async def mcp_command(context: CliContext, text: str) -> None:
    parts = shlex.split(text)
    registry = McpRegistry(
        context.agent.policy, layout=ProjectLayout.discover(context.agent.workspace)
    )
    try:
        if len(parts) == 1 or parts[1] == "list":
            servers = await asyncio.to_thread(registry.servers)
            for server in servers.values():
                context.console.print(
                    f"{server.name} scope={server.scope} enabled={server.enabled} tools={','.join(server.allowed_tools)}"
                )
        elif len(parts) == 3 and parts[1] in {"status", "tools"}:
            server = await asyncio.to_thread(registry.get, parts[2])
            context.console.print(
                f"{server.name} cwd={server.cwd} enabled={server.enabled} tools={','.join(server.allowed_tools)}"
            )
        else:
            raise ValueError("usage: /mcp [list|status <server>|tools <server>]")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def show_git_diff(context: CliContext) -> None:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(context.agent.workspace),
        "diff",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    output = stdout if process.returncode == 0 else stderr
    context.console.print(
        output.decode("utf-8", "replace"), markup=False, highlight=False
    )
