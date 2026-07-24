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
from .prompt import select_model, select_permission_mode


async def render_approvals(context: CliContext) -> None:
    items = await context.require_queries().actions(
        context.session.session_id,
        statuses={ActionStatus.PENDING, ActionStatus.APPROVED, ActionStatus.RUNNING},
    )
    render_approval_view(context.console, items)


async def render_sources(context: CliContext) -> None:
    render_source_view(
        context.console,
        await context.require_queries().sources(context.session.session_id),
    )


async def approve_action(context: CliContext, prefix: str) -> None:
    coordinator = context.session.action_factory("cli")
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
        await context.session.workflow.settle_approval(
            context.session.session_id, action.run_id
        )
        await _resume_paused_action(context, action.run_id)
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def reject_action(context: CliContext, prefix: str) -> None:
    coordinator = context.session.action_factory("cli")
    try:
        action = await coordinator.resolve(prefix)
        await coordinator.for_run(action.run_id).reject(action.id)
        await context.session.workflow.settle_approval(
            context.session.session_id, action.run_id
        )
        await _resume_paused_action(context, action.run_id)
        context.console.print(f"[warning]Rejected {action.type.value}.[/]")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def apply_action_decision(
    context: CliContext, action: ActionRecord, decision: str
) -> None:
    coordinator = context.session.action_factory("cli").for_run(action.run_id)
    if decision == "later":
        context.console.print(f"[waiting]Action remains pending:[/] {action.id[:12]}")
        return
    if decision == "reject":
        await coordinator.reject(action.id)
        await context.session.workflow.settle_approval(
            context.session.session_id, action.run_id
        )
        await _resume_paused_action(context, action.run_id)
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
    await context.session.workflow.settle_approval(
        context.session.session_id, action.run_id
    )
    await _resume_paused_action(context, action.run_id)


async def _resume_paused_action(context: CliContext, run_id: str) -> None:
    run = await context.session.runs.require(
        run_id, session_id=context.session.session_id
    )
    if run.status != "waiting_approval":
        return
    async for event in context.session.resume_paused_stream(run_id):
        if event.kind.value == "text_delta":
            context.console.print(str(event.data.get("text", "")), end="")


async def undo(context: CliContext) -> None:
    try:
        action = await context.session.action_factory("cli").reverse_last_file_action()
        context.console.print(f"[success]Undone:[/] {action.request.get('path')}")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def set_permission_mode(context: CliContext, value: str) -> None:
    try:
        mode = PermissionMode.parse(value)
        await context.session.persist_permission_mode(mode)
        context.console.print(f"[success]Permission mode:[/] {mode.value}")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def permissions(context: CliContext, text: str) -> None:
    parts = shlex.split(text)
    if len(parts) == 1:
        try:
            selected = await asyncio.to_thread(
                select_permission_mode, context.session.permission_mode
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


async def set_model(context: CliContext, value: str) -> None:
    try:
        model = await context.session.set_model(value)
        context.console.print(f"[success]Model:[/] {model}")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def model_command(context: CliContext, text: str) -> None:
    parts = shlex.split(text)
    if len(parts) == 1:
        try:
            selected = await asyncio.to_thread(select_model, context.session.model)
        except (EOFError, KeyboardInterrupt):
            context.console.print("[waiting]Model unchanged.[/]")
            return
        await set_model(context, selected)
        return
    if len(parts) != 2:
        context.console.print(
            "[error]Usage:[/] /model [deepseek-v4-flash|deepseek-v4-pro]"
        )
        return
    await set_model(context, parts[1])


async def mcp_command(context: CliContext, text: str) -> None:
    parts = shlex.split(text)
    registry = McpRegistry(
        context.session.policy, layout=ProjectLayout.discover(context.session.workspace)
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
        str(context.session.workspace),
        "diff",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    output = stdout if process.returncode == 0 else stderr
    context.console.print(
        output.decode("utf-8", "replace"), markup=False, highlight=False
    )
