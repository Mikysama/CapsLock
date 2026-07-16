"""Slash-command routing for the interactive CLI."""

from __future__ import annotations

from collections.abc import Callable

from . import actions
from .memory import memory_command
from .commands import resolve_command
from .context import CliContext
from .render import render_command_tree, render_status


CommandHandler = Callable[[str], None]


def dispatch_slash_command(context: CliContext, text: str) -> str:
    if text == "/":
        return "handled"
    spec = resolve_command(text)
    if spec is None:
        render_command_tree(context.console, text)
        context.console.print("[warning]请选择列表中的完整指令，或输入 / 后从菜单选择。[/]")
        return "handled"
    if spec.handler == "exit":
        return "exit"
    handlers(context)[spec.handler](text)
    return "handled"


def handlers(context: CliContext) -> dict[str, CommandHandler]:
    agent, console = context.agent, context.console

    def status(_: str) -> None:
        render_status(console, agent)

    def stored_context(_: str) -> None:
        count = len(agent.store.messages(agent.session_id, agent.max_context_messages))
        console.print(
            f"[text.secondary]Stored context messages:[/] "
            f"[text.muted]{count}/{agent.max_context_messages}[/]"
        )

    return {
        "help": lambda _: render_command_tree(console, "/"),
        "status": status,
        "rename": lambda value: actions.rename_session(context, value),
        "permissions": lambda value: actions.permissions(context, value),
        "context": stored_context,
        "cost": lambda _: actions.render_cost(context),
        "tasks": lambda _: actions.render_tasks(context),
        "changes": lambda _: actions.render_changes(context),
        "commands": lambda _: actions.render_commands(context),
        "web": lambda _: actions.render_external_actions(context, kinds={"web_search", "web_fetch"}),
        "sources": lambda _: actions.render_sources(context),
        "memory": lambda value: memory_command(context, value),
        "mcp": lambda value: actions.mcp_command(context, value),
        "approve": lambda value: actions.approve_action(context, value.partition(" ")[2].strip()),
        "reject": lambda value: actions.reject_action(context, value.partition(" ")[2].strip()),
        "undo": lambda _: actions.undo(context),
        "diff": lambda _: actions.show_git_diff(context),
        "clear": lambda _: console.print(
            "[text.secondary]This session is append-only. Start[/] [command]capslock[/] "
            "[text.secondary]to create a fresh session.[/]"
        ),
        "cancel": lambda _: console.print(
            "[text.secondary]No background run is active. Press[/] [text.muted]Ctrl-C[/] "
            "[text.secondary]while a request is running to cancel it.[/]"
        ),
    }
