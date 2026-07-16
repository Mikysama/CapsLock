"""CLI adapters for permissions and coordinated workspace actions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..application import ActionCoordinator
from ..changes import make_diff
from ..config import CommandSettings, McpSettings, WebSettings
from ..domain import ActionType
from ..mcp import McpRegistry
from ..permissions import PermissionMode
from ..policy import PolicyError, WorkspacePolicy
from ..runtime import AgentRuntimeError, WorkspaceAgent
from ..security import redact
from .context import CliContext
from .render import (
    permission_badge,
    render_change_approval,
    render_changes as render_change_list,
    render_command_approval,
    render_commands as render_command_list,
    render_command_result,
    render_cost as render_cost_summary,
    render_external_approval,
    render_external_actions as render_external_action_list,
    render_external_result,
    render_git_diff,
    render_mcp_server,
    render_mcp_servers,
    render_pending_external_action,
    render_session_renamed,
    render_sources as render_source_list,
    render_tasks as render_task_list,
    render_undo_preview,
    select_choice,
)


def action_coordinator(agent: WorkspaceAgent, run_id: str = "cli") -> ActionCoordinator:
    return ActionCoordinator(
        store=agent.store,
        policy=WorkspacePolicy(agent.workspace),
        session_id=agent.session_id,
        run_id=run_id,
        event=agent.events.emit,
        permission_mode=agent.permission_mode,
        command=CommandSettings(agent.command_timeout_seconds, agent.command_output_bytes),
        web=WebSettings(
            agent.tavily_api_key,
            agent.web_timeout_seconds,
            agent.web_max_bytes,
            agent.web_max_redirects,
        ),
        mcp=McpSettings(agent.mcp_timeout_seconds, agent.mcp_output_bytes),
    )


def render_changes(context: CliContext, *, pending_only: bool = False) -> None:
    statuses = ("pending",) if pending_only else None
    changes = context.agent.store.list_changes(context.agent.session_id, statuses=statuses)
    render_change_list(context.console, changes, pending_only=pending_only)


def render_commands(context: CliContext) -> None:
    agent = context.agent
    render_command_list(context.console, agent.store.list_commands(agent.session_id))


def render_tasks(context: CliContext) -> None:
    agent = context.agent
    render_task_list(context.console, agent.store.list_tasks(agent.session_id))


def render_cost(context: CliContext) -> None:
    agent = context.agent
    render_cost_summary(context.console, *agent.store.session_cost(agent.session_id))


def render_external_actions(context: CliContext, *, kinds: set[str] | None = None) -> None:
    agent = context.agent
    actions = [
        item
        for item in agent.store.list_external_actions(agent.session_id)
        if kinds is None or item.kind in kinds
    ]
    render_external_action_list(context.console, actions)


def render_sources(context: CliContext) -> None:
    agent = context.agent
    render_source_list(context.console, agent.store.list_sources(agent.session_id))


def rename_session(context: CliContext, command: str) -> None:
    title = command.partition(" ")[2]
    try:
        session = context.agent.store.rename_session(context.agent.session_id, title)
        render_session_renamed(context.console, session)
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


def approve_action(context: CliContext, action_id: str) -> None:
    try:
        action = action_coordinator(context.agent).resolve(action_id)
        if action.type is ActionType.COMMAND:
            approve_command(context, action.id)
        elif action.type in {
            ActionType.WEB_SEARCH,
            ActionType.WEB_FETCH,
            ActionType.MCP_CONNECT,
            ActionType.MCP_CALL,
        }:
            approve_external(context, action.id)
        else:
            approve_change(context, action.id)
    except (ValueError, PolicyError) as exc:
        context.console.print(f"[error]Error:[/] {exc}")


def approve_change(context: CliContext, change_id: str) -> None:
    if not change_id:
        context.console.print("[error]Error:[/] provide a change id from [command]/changes[/]")
        return
    agent, console = context.agent, context.console
    try:
        action = action_coordinator(agent).resolve(
            change_id,
            types={ActionType.FILE_EDIT, ActionType.FILE_CREATE},
        )
        change = agent.store.get_change(action.id, session_id=agent.session_id)
        assert change is not None
        render_change_approval(console, change)
        if console.input("Apply this change? [y/N] ").strip().casefold() not in {"y", "yes"}:
            console.print("[waiting]Change remains pending.[/]")
            return
        applied = action_coordinator(agent, change.run_id).approve_and_execute(change.id)
        console.print(
            f"[success]Applied:[/] [path]{applied.path}[/]. Review with [command]/diff[/]; "
            "use [command]/undo[/] to revert."
        )
    except (AgentRuntimeError, ValueError, PolicyError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def approve_command(context: CliContext, command_id: str) -> None:
    agent, console = context.agent, context.console
    try:
        action = action_coordinator(agent).resolve(command_id, types={ActionType.COMMAND})
        command = agent.store.get_command(action.id, session_id=agent.session_id)
        assert command is not None
        render_command_approval(console, command)
        if console.input("Run this command? [y/N] ").strip().casefold() not in {"y", "yes"}:
            console.print("[waiting]Command remains pending.[/]")
            return
        result = action_coordinator(agent, command.run_id).approve_and_execute(command.id)
        render_command_result(console, result)
    except (AgentRuntimeError, ValueError, PolicyError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def reject_action(context: CliContext, action_id: str) -> None:
    try:
        coordinator = action_coordinator(context.agent)
        action = coordinator.resolve(action_id)
        coordinator.for_run(action.run_id).reject(action.id)
        context.console.print(f"[warning]Rejected {action.type.value} proposal.[/]")
    except (AgentRuntimeError, ValueError, PolicyError) as exc:
        context.console.print(f"[error]Error:[/] {exc}")


def undo(context: CliContext) -> None:
    agent, console = context.agent, context.console
    try:
        change = agent.store.last_applied_change(agent.session_id)
        if change is None:
            raise ValueError("no applied change is available to undo")
        reverse = make_diff(Path(change.path), change.after_content, change.before_content or "")
        render_undo_preview(console, change, reverse)
        if console.input("Undo this change? [y/N] ").strip().casefold() not in {"y", "yes"}:
            return
        undone = action_coordinator(agent, change.run_id).reverse_last_file_action()
        console.print(f"[success]Undone:[/] [path]{undone.path}[/]")
    except (ValueError, PolicyError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def show_git_diff(context: CliContext) -> None:
    from ..tools import RunContext, workspace_tools

    agent = context.agent
    run_context = RunContext(
        session_id=agent.session_id,
        run_id="cli",
        policy=WorkspacePolicy(agent.workspace),
        event=agent.events.emit,
        store=agent.store,
    )
    result, _ = workspace_tools().invoke("git_diff", run_context, {})
    output = result.data.get("output", "") if result.ok and isinstance(result.data, dict) else result.error
    render_git_diff(context.console, output, success=result.ok)


def permissions(context: CliContext, text: str) -> None:
    agent = context.agent
    parts = text.split()
    if len(parts) == 1:
        choices = (
            ("full", "完全访问：不确认；保留审计与文件撤销"),
            ("approve", "为我批准：仅文件、命令、MCP 等高风险动作确认"),
            ("ask", "每次确认：每条请求及每个动作都确认"),
            ("cancel", "保持当前模式"),
        )
        default = {
            PermissionMode.FULL_ACCESS: 0,
            PermissionMode.APPROVE_FOR_ME: 1,
            PermissionMode.ASK_FOR_APPROVAL: 2,
        }[agent.permission_mode]
        choice = select_choice(
            context.console,
            "选择权限模式",
            choices,
            default=default,
            escape_key="cancel",
        )
        if choice == "cancel":
            context.console.print(
                f"[text.secondary]权限模式未改变：[/][text.muted]{agent.permission_mode.value}[/]"
            )
            return
        set_permission_mode(context, choice)
        return
    if len(parts) != 2:
        context.console.print("[error]Usage:[/] [command]/permissions [full|approve|ask][/]")
        return
    set_permission_mode(context, parts[1])


def set_permission_mode(context: CliContext, value: str) -> None:
    try:
        mode = PermissionMode.parse(value)
        context.agent.permission_mode = mode
        context.agent.store.set_workspace_setting("permission_mode", mode.value)
        context.console.print(
            f"[success]权限模式已切换：[/] {permission_badge(mode)} [text.muted]({mode.value})[/]"
        )
        context.console.print("[text.secondary]该选择已保存到当前工作区。[/]")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


def approve_external(context: CliContext, action_id: str) -> None:
    agent, console = context.agent, context.console
    try:
        common = action_coordinator(agent).resolve(
            action_id,
            types={
                ActionType.WEB_SEARCH,
                ActionType.WEB_FETCH,
                ActionType.MCP_CONNECT,
                ActionType.MCP_CALL,
            },
        )
        action = agent.store.get_external_action(common.id, session_id=agent.session_id)
        assert action is not None
        render_external_approval(console, action, redact(action.payload))
        choice = select_choice(
            console,
            "Run this external action?",
            (("approve", "Approve and run"), ("later", "Keep pending")),
            escape_key="later",
        )
        if choice != "approve":
            console.print("[waiting]Action remains pending.[/]")
            return
        execute_external(context, action)
    except (AgentRuntimeError, ValueError, PolicyError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def review_pending_external_actions(context: CliContext, run_id: str) -> list[object]:
    agent, console = context.agent, context.console
    pending = [
        item
        for item in agent.store.list_external_actions(agent.session_id)
        if item.run_id == run_id and item.status == "pending"
    ]
    completed: list[object] = []
    for action in pending:
        render_pending_external_action(console, action, redact(action.payload))
        choice = select_choice(
            console,
            "Choose an action",
            (("approve", "Approve and run"), ("reject", "Reject"), ("later", "Decide later")),
            escape_key="later",
        )
        try:
            if choice == "approve":
                result = execute_external(context, action)
                if result is not None and result.status == "completed":
                    completed.append(result)
            elif choice == "reject":
                action_coordinator(agent, action.run_id).reject(action.id)
                console.print(f"[warning]Rejected:[/] {action.id[:12]} · {action.kind}")
            else:
                console.print(
                    f"[waiting]Left pending:[/] [text.muted]{action.id[:12]}[/] "
                    "[text.secondary]Use /approve or /reject later.[/]"
                )
        except (AgentRuntimeError, ValueError, PolicyError) as exc:
            console.print(f"[error]Error:[/] {exc}")
    return completed


def execute_external(context: CliContext, action: Any) -> Any:
    result = action_coordinator(context.agent, action.run_id).approve_and_execute(action.id)
    render_external_result(context.console, result, redact(result.result))
    return result


def mcp_command(context: CliContext, text: str) -> None:
    parts = text.split()
    registry = McpRegistry(WorkspacePolicy(context.agent.workspace))
    try:
        if len(parts) == 1 or parts[1] == "list":
            servers = registry.servers()
            render_mcp_servers(context.console, list(servers.values()))
        elif parts[1] in {"status", "tools"} and len(parts) == 3:
            server = registry.get(parts[2])
            render_mcp_server(context.console, server)
        else:
            context.console.print(
                "[error]Usage:[/] [command]/mcp list | /mcp status <server> | /mcp tools <server>[/]"
            )
    except (ValueError, PolicyError) as exc:
        context.console.print(f"[error]Error:[/] {exc}")
