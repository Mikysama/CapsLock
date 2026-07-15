"""Interactive and one-shot CLI for the read-only workspace agent."""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.menus import CompletionsMenu, MultiColumnCompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.shortcuts import CompleteStyle
from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .changes import ChangeService, make_diff
from .execution import CommandService
from .external import WebService
from .mcp import McpRegistry, McpService
from .config import Settings
from .environment import load_project_environment
from .policy import PolicyError, WorkspacePolicy
from .permissions import PermissionMode
from .runtime import AgentRuntimeError, WorkspaceAgent
from .session import SessionStore
from .theme import build_prompt_style, make_console


console = make_console()


_CAPSLOCK_FONT = {
    "C": ("⇪⇪⇪", "⇪  ", "⇪  ", "⇪  ", "⇪⇪⇪"),
    "A": ("⇪⇪⇪", "⇪ ⇪", "⇪⇪⇪", "⇪ ⇪", "⇪ ⇪"),
    "P": ("⇪⇪⇪", "⇪ ⇪", "⇪⇪⇪", "⇪  ", "⇪  "),
    "S": ("⇪⇪⇪", "⇪  ", "⇪⇪⇪", "  ⇪", "⇪⇪⇪"),
    "L": ("⇪  ", "⇪  ", "⇪  ", "⇪  ", "⇪⇪⇪"),
    "O": ("⇪⇪⇪", "⇪ ⇪", "⇪ ⇪", "⇪ ⇪", "⇪⇪⇪"),
    "K": ("⇪ ⇪", "⇪⇪ ", "⇪  ", "⇪⇪ ", "⇪ ⇪"),
}
CAPSLOCK_ART = tuple(
    "  ".join(_CAPSLOCK_FONT[letter][row] for letter in "CAPSLOCK")
    for row in range(5)
)


def _capslock_logo() -> Text:
    """Build the CapsLock wordmark from Caps Lock symbol pixels."""
    logo = Text(no_wrap=True)
    for index, line in enumerate(CAPSLOCK_ART):
        logo.append(line, style="primary.bold")
        if index < len(CAPSLOCK_ART) - 1:
            logo.append("\n")
    return logo


def _startup_banner(agent: WorkspaceAgent, width: int | None = None) -> Panel:
    """Return a Claude-style startup card with a compact narrow-terminal fallback."""
    terminal_width = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    identity = Text.assemble(
        ("Welcome back!\n", "text.primary.bold"),
        (f"{agent.model}", "text.secondary"),
        ("  ·  ", "text.muted"),
        (agent.permission_mode.value, "waiting"),
        (f"\n{agent.workspace}", "path"),
    )
    tips = Text.assemble(
        ("Tips for getting started\n", "primary.bold"),
        ("Type ", "text.primary"),
        ("/", "command.bold"),
        (" to browse commands and ", "text.primary"),
        ("/status", "command.bold"),
        (" to inspect this session.\n\n", "text.primary"),
        ("CapsLock keeps workspace actions visible, reviewable, and reversible.", "text.secondary"),
    )

    if terminal_width < 110:
        body = Group(Align.center(identity), Align.center(_capslock_logo()))
    else:
        body = Table.grid(expand=True, padding=(0, 2))
        body.add_column(ratio=2)
        body.add_column(ratio=3)
        body.add_row(Align.center(Group(identity, Text(""), _capslock_logo()), vertical="middle"), tips)

    title = Text(" CapsLock ", style="agent.bold")
    return Panel(body, title=title, title_align="left", border_style="border.focus", padding=(1, 2))


@dataclass(frozen=True)
class CommandNode:
    command: str
    description: str
    children: tuple["CommandNode", ...] = ()


COMMAND_TREE = (
    CommandNode("/help", "显示可用指令"),
    CommandNode("/status", "显示会话、模型和权限模式"),
    CommandNode(
        "/permissions",
        "查看或切换权限模式",
        (
            CommandNode("/permissions full", "完全访问：自动执行，保留审计与回滚"),
            CommandNode("/permissions approve", "仅确认高风险动作"),
            CommandNode("/permissions ask", "确认每次请求和动作"),
        ),
    ),
    CommandNode("/context", "显示已保留的上下文数量"),
    CommandNode("/cost", "显示本会话 token 与费用"),
    CommandNode("/tasks", "显示任务清单"),
    CommandNode("/changes", "查看待审文件变更"),
    CommandNode("/commands", "查看待审固定命令"),
    CommandNode("/web", "查看 Web 动作提案"),
    CommandNode("/sources", "查看已保存的外部来源"),
    CommandNode(
        "/mcp",
        "管理本地 MCP 服务",
        (
            CommandNode("/mcp list", "列出 MCP 服务"),
            CommandNode("/mcp status", "显示服务状态；需 server 名称"),
            CommandNode("/mcp tools", "显示允许工具；需 server 名称"),
        ),
    ),
    CommandNode("/approve", "批准变更、命令或外部动作；需 ID"),
    CommandNode("/reject", "拒绝变更、命令或外部动作；需 ID"),
    CommandNode("/undo", "预览并撤销最后一次 CapsLock 文件变更"),
    CommandNode("/diff", "显示当前 Git 工作树差异"),
    CommandNode("/clear", "说明如何开始新会话"),
    CommandNode("/cancel", "说明如何取消当前运行"),
    CommandNode("/exit", "退出聊天"),
)


def _permission_badge(mode: PermissionMode) -> str:
    """Return the persistent, terminal-safe indicator shown before every prompt."""
    return {
        PermissionMode.FULL_ACCESS: "[error.bold]权限：完全访问[/]",
        PermissionMode.APPROVE_FOR_ME: "[waiting.bold]权限：高风险确认[/]",
        PermissionMode.ASK_FOR_APPROVAL: "[thinking.bold]权限：每次确认[/]",
    }[mode]


def _matching_command_nodes(prefix: str, nodes: tuple[CommandNode, ...] = COMMAND_TREE) -> tuple[CommandNode, ...]:
    """Filter the command tree while retaining ancestors of matching descendants."""
    normalized = prefix.casefold().rstrip()
    matches: list[CommandNode] = []
    for node in nodes:
        children = _matching_command_nodes(prefix, node.children)
        if node.command.casefold().startswith(normalized) or normalized.startswith(node.command.casefold()) or children:
            matches.append(CommandNode(node.command, node.description, children))
    return tuple(matches)


def _command_completions(prefix: str) -> list[str]:
    normalized = prefix.casefold().rstrip()
    found: list[str] = []

    def visit(nodes: tuple[CommandNode, ...]) -> None:
        for node in nodes:
            if node.command.casefold().startswith(normalized):
                found.append(node.command)
            visit(node.children)

    visit(COMMAND_TREE)
    return found


def _render_command_tree(prefix: str = "/") -> None:
    """Render a Claude-style, fixed two-column command list."""
    matches = _command_completions(prefix)
    descriptions = _command_descriptions()
    if not matches:
        console.print("[warning]没有匹配的指令。[/]")
        return
    table = Table.grid(padding=(0, 4))
    table.add_column(style="command", no_wrap=True)
    table.add_column(style="text.secondary")
    for command in matches:
        table.add_row(command, descriptions[command])
    console.print(table)


def _command_descriptions(nodes: tuple[CommandNode, ...] = COMMAND_TREE) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in nodes:
        found[node.command] = node.description
        found.update(_command_descriptions(node.children))
    return found


class SlashCommandCompleter(Completer):
    """Prefix-filtered slash commands displayed as a Claude-style two-column list."""

    def get_completions(self, document: Document, complete_event: object):
        prefix = document.text_before_cursor
        if not prefix.startswith("/"):
            return
        descriptions = _command_descriptions()
        matches = _command_completions(prefix)
        for command in matches:
            yield Completion(
                command,
                start_position=-len(prefix),
                display=FormattedText([("class:command-name", command)]),
                display_meta=descriptions[command],
            )


class SlashCommandLexer(Lexer):
    """Highlight complete slash-command input, not merely the prompt label."""

    def lex_document(self, document: Document):
        def get_line(line_number: int):
            line = document.lines[line_number]
            return [("class:slash-command", line)] if line.startswith("/") else [("class:user-input", line)]

        return get_line


PROMPT_STYLE = build_prompt_style()


def _prompt_tokens(mode: PermissionMode, width: int | None = None) -> FormattedText:
    terminal_width = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    return FormattedText([
        ("class:input-border", "─" * max(20, terminal_width - 1)),
        ("", "\n"),
        ("class:prompt", "❯ "),
    ])


def _permission_rprompt(mode: PermissionMode) -> FormattedText:
    return FormattedText([("class:permission", f"{mode.value} ")])


def _prompt_footer() -> FormattedText:
    return FormattedText([("class:footer", "? /help  ·  ↑/↓ 选择  ·  Tab 补全")])


def _refresh_slash_completion(buffer: object) -> None:
    """Reopen completion after edits such as Backspace that cancel its state."""
    prefix = buffer.document.text_before_cursor
    if prefix.startswith("/"):
        buffer.start_completion(select_first=False)
    else:
        buffer.cancel_completion()


SLASH_KEY_BINDINGS = KeyBindings()


@SLASH_KEY_BINDINGS.add("backspace")
def _backspace_and_refresh(event: object) -> None:
    event.current_buffer.delete_before_cursor()
    _refresh_slash_completion(event.current_buffer)


@SLASH_KEY_BINDINGS.add("delete")
def _delete_and_refresh(event: object) -> None:
    event.current_buffer.delete()
    _refresh_slash_completion(event.current_buffer)


def _anchor_completion_menus(container: object) -> None:
    """Pin completion lists to the left edge instead of following the cursor."""
    seen: set[int] = set()

    def visit(node: object) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        for floating in getattr(node, "floats", ()):
            if isinstance(floating.content, (CompletionsMenu, MultiColumnCompletionsMenu)):
                floating.xcursor = False
                floating.left = 0
        get_children = getattr(node, "get_children", None)
        if get_children is not None:
            for child in get_children():
                visit(child)

    visit(container)


def _prompt_session() -> PromptSession[str]:
    session = PromptSession(
        completer=SlashCommandCompleter(),
        lexer=SlashCommandLexer(),
        key_bindings=SLASH_KEY_BINDINGS,
        style=PROMPT_STYLE,
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        reserve_space_for_menu=16,
    )
    _anchor_completion_menus(session.app.layout.container)
    return session


def _select_command() -> str | None:
    descriptions = _command_descriptions()
    commands = _command_completions("/")
    _render_command_tree("/")
    options = tuple((command, f"{command} — {descriptions[command]}") for command in commands)
    choice = _select_choice("选择指令", options + (("cancel", "取消"),), escape_key="cancel")
    return None if choice == "cancel" else choice


def _select_choice(title: str, options: tuple[tuple[str, str], ...], *, default: int = 0, escape_key: str | None = None) -> str:
    if not options:
        raise ValueError("selection options must not be empty")
    escape_key = escape_key or options[-1][0]
    console.print(_selection_menu(title, options))
    answer = console.input(f"[primary.bold]选择 1-{len(options)}（Enter 默认 {default + 1}）>[/] ").strip().casefold()
    if not answer:
        return options[default][0]
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1][0]
    for key, label in options:
        if answer in {key.casefold(), key[0].casefold(), label.casefold()}:
            return key
    console.print("[warning]未识别的选择，已取消。[/]")
    return escape_key


def _selection_menu(title: str, options: tuple[tuple[str, str], ...]) -> Panel:
    body = Text()
    for index, (_, label) in enumerate(options, start=1):
        body.append(f"{index}. ", style="primary.bold")
        body.append(f"{label}\n", style="text.primary")
    return Panel(body, title=title, title_align="left", border_style="border.focus")


def _table(*columns: str) -> Table:
    """Create a transparent table with theme-controlled hierarchy."""
    return Table(
        *columns,
        header_style="primary.soft.bold",
        border_style="border.muted",
        row_styles=("text.primary",),
    )


def _status_text(status: str) -> Text:
    """Map persisted status values to the closest semantic theme role."""
    normalized = status.casefold()
    if normalized in {"completed", "applied", "approved", "success", "succeeded"}:
        style = "success"
    elif normalized in {"failed", "error", "rejected", "cancelled"}:
        style = "error"
    elif normalized in {"running", "executing", "in_progress"}:
        style = "running"
    elif normalized in {"pending", "waiting", "proposed"}:
        style = "waiting"
    else:
        style = "text.secondary"
    return Text(status, style=style)


def _move_selection(selected: int, key: str, option_count: int) -> int:
    """Compatibility helper retained for callers that use arrow-style selection."""
    if key.casefold() in {"up", "left", "k"}:
        return (selected - 1) % option_count
    if key.casefold() in {"down", "right", "j"}:
        return (selected + 1) % option_count
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capslock", description="Trustworthy read-only workspace agent.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace root (default: current directory)")
    parser.add_argument("--debug", action="store_true", help="Show runtime events after each answer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("chat", help="Start a new interactive session")
    ask = subparsers.add_parser("ask", help="Ask one question and exit")
    ask.add_argument("question")
    resume = subparsers.add_parser("resume", help="Resume a saved interactive session")
    resume.add_argument("session_id")
    sessions = subparsers.add_parser("sessions", help="List saved sessions")
    sessions.add_argument("--limit", type=int, default=20)
    subparsers.add_parser("doctor", help="Check local configuration and workspace access")
    return parser


def _store(workspace: Path) -> SessionStore:
    return SessionStore(workspace / ".capslock" / "capslock.sqlite3")


def _client(settings: Settings) -> OpenAI:
    if not settings.api_key or settings.api_key.startswith("your_"):
        raise AgentRuntimeError("API key is not set; use CAPSLOCK_API_KEY or DEEPSEEK_API_KEY in your environment or .env")
    return OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=settings.timeout_seconds)


def _agent(workspace: Path, settings: Settings, session_id: str | None = None) -> WorkspaceAgent:
    store = _store(workspace)
    mode = PermissionMode.parse(store.workspace_setting("permission_mode", settings.permission_mode) or settings.permission_mode)
    return WorkspaceAgent(_client(settings), workspace=workspace, model=settings.model, store=store, session_id=session_id, max_turns=settings.max_turns, max_context_messages=settings.max_context_messages, command_timeout_seconds=settings.command_timeout_seconds, command_output_bytes=settings.command_output_bytes, input_cost_per_million=settings.input_cost_per_million, output_cost_per_million=settings.output_cost_per_million, tavily_api_key=settings.tavily_api_key, web_timeout_seconds=settings.web_timeout_seconds, web_max_bytes=settings.web_max_bytes, web_max_redirects=settings.web_max_redirects, mcp_timeout_seconds=settings.mcp_timeout_seconds, mcp_output_bytes=settings.mcp_output_bytes, permission_mode=mode)


def main(argv: list[str] | None = None) -> int:
    load_project_environment()
    args = build_parser().parse_args(argv)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        console.print(f"[error]Error:[/] [path]{workspace}[/]")
        return 2
    settings = Settings.load(workspace)
    try:
        if args.command == "doctor":
            return _doctor(workspace, settings)
        if args.command == "sessions":
            return _sessions(_store(workspace), args.limit)
        agent = _agent(workspace, settings, getattr(args, "session_id", None))
        if args.command == "ask":
            with console.status("[running.bold]Agent is analyzing the workspace...[/]"):
                answer = agent.ask(args.question)
            _render(answer, args.debug)
            return 0
        return _chat(agent, args.debug)
    except KeyboardInterrupt:
        console.print("\n[warning]Cancelled.[/]")
        return 130
    except AgentRuntimeError as exc:
        console.print(f"[error]Error:[/] {exc}")
        return 2
    except Exception as exc:
        console.print(f"[error]Model or transport error:[/] {exc}")
        return 1


def _chat(agent: WorkspaceAgent, debug: bool) -> int:
    console.print(_startup_banner(agent))
    prompt_session = _prompt_session()
    while True:
        try:
            question = prompt_session.prompt(
                _prompt_tokens(agent.permission_mode),
                rprompt=_permission_rprompt(agent.permission_mode),
                bottom_toolbar=_prompt_footer(),
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if question == "/":
            continue
        if question in {"/exit", "/quit"}:
            return 0
        if question == "/help":
            _render_command_tree("/")
            continue
        if question in {"/status", "/session"}:
            console.print(Text.assemble(
                ("session=", "text.muted"), (f"{agent.session_id}\n", "text.secondary"),
                ("workspace=", "text.muted"), (f"{agent.workspace}\n", "path"),
                ("model=", "text.muted"), (f"{agent.model}\n", "text.secondary"),
                ("max_turns=", "text.muted"), (f"{agent.max_turns}\n", "text.secondary"),
                ("permission_mode=", "text.muted"), (agent.permission_mode.value, "waiting"),
            ))
            continue
        if question == "/permissions" or question.startswith("/permissions "):
            _permissions(agent, question)
            continue
        if question == "/context":
            console.print(f"[text.secondary]Stored context messages:[/] [text.muted]{len(agent.store.messages(agent.session_id, agent.max_context_messages))}/{agent.max_context_messages}[/]")
            continue
        if question == "/cost":
            _render_cost(agent)
            continue
        if question == "/tasks":
            _render_tasks(agent)
            continue
        if question == "/clear":
            console.print("[text.secondary]This session is append-only. Start[/] [command]capslock chat[/] [text.secondary]to create a fresh session.[/]")
            continue
        if question == "/changes":
            _render_changes(agent)
            continue
        if question == "/commands":
            _render_commands(agent)
            continue
        if question == "/web":
            _render_external_actions(agent, kinds={"web_search", "web_fetch"})
            continue
        if question == "/sources":
            _render_sources(agent)
            continue
        if question.startswith("/mcp"):
            _mcp_command(agent, question)
            continue
        if question.startswith("/approve "):
            _approve_action(agent, question.removeprefix("/approve ").strip())
            continue
        if question.startswith("/reject "):
            _reject_action(agent, question.removeprefix("/reject ").strip())
            continue
        if question == "/undo":
            _undo(agent)
            continue
        if question == "/diff":
            _show_git_diff(agent)
            continue
        if question == "/cancel":
            console.print("[text.secondary]No background run is active. Press[/] [text.muted]Ctrl-C[/] [text.secondary]while a request is running to cancel it.[/]")
            continue
        if question.startswith("/"):
            _render_command_tree(question)
            console.print("[warning]请选择树中的完整指令，或输入 / 后从菜单选择。[/]")
            continue
        if not question:
            continue
        if agent.permission_mode is PermissionMode.ASK_FOR_APPROVAL:
            choice = _select_choice(
                "Send this request to CapsLock?",
                (("approve", "Approve and send"), ("reject", "Do not send")),
                escape_key="reject",
            )
            if choice != "approve":
                console.print("[warning]Request not sent.[/]")
                continue
        try:
            _run_chat_turn(agent, question, debug)
        except AgentRuntimeError as exc:
            console.print(f"[error]Error:[/] {exc}")


def _run_chat_turn(agent: WorkspaceAgent, question: str, debug: bool) -> None:
    while True:
        with console.status("[running.bold]Agent is analyzing the workspace...[/]"):
            answer = agent.ask(question)
        _render(answer, debug)
        _render_changes(agent, pending_only=True)
        completed = _review_pending_external_actions(agent, answer.run_id)
        if not completed:
            return
        if all(item.kind in {"web_search", "web_fetch"} for item in completed):
            question = (
                "The user approved the Web action and it completed. Call list_external_sources, "
                "then continue the user's previous request using those sources. Do not propose the same search again."
            )
        else:
            return


def _render(answer: object, debug: bool) -> None:
    from .runtime import WorkspaceAnswer
    assert isinstance(answer, WorkspaceAnswer)
    console.print(f"\n[agent.bold]◆ Agent[/] {answer.text}")
    if answer.citations:
        for item in answer.citations:
            if hasattr(item, "path"):
                console.print(f"  [text.secondary]Evidence:[/] [path]{item.path}[/]:L{item.start_line}-L{item.end_line} [text.muted]({item.id})[/]")
            else:
                console.print(f"  [text.secondary]Source:[/] {item.title} · {item.url} · [text.muted]{item.fetched_at} ({item.id})[/]")
    console.print(f"  [text.muted]Run {answer.run_id[:8]} · {answer.duration_ms}ms[/]")
    if debug:
        for event in answer.events:
            console.print(f"  [text.muted]{event.kind}: {event.data}[/]")


def _change_service(agent: WorkspaceAgent) -> ChangeService:
    return ChangeService(agent.store, WorkspacePolicy(agent.workspace), agent.session_id, "cli", agent.events.emit)


def _command_service(agent: WorkspaceAgent) -> CommandService:
    return CommandService(agent.store, WorkspacePolicy(agent.workspace), agent.session_id, "cli", agent.events.emit, timeout_seconds=agent.command_timeout_seconds, output_limit_bytes=agent.command_output_bytes)


def _web_service(agent: WorkspaceAgent, run_id: str = "cli") -> WebService:
    return WebService(agent.store, agent.session_id, run_id, agent.events.emit, tavily_api_key=agent.tavily_api_key, timeout_seconds=agent.web_timeout_seconds, max_bytes=agent.web_max_bytes, max_redirects=agent.web_max_redirects)


def _mcp_service(agent: WorkspaceAgent, run_id: str = "cli") -> McpService:
    return McpService(agent.store, WorkspacePolicy(agent.workspace), agent.session_id, run_id, agent.events.emit, timeout_seconds=agent.mcp_timeout_seconds, output_limit_bytes=agent.mcp_output_bytes)


def _render_changes(agent: WorkspaceAgent, *, pending_only: bool = False) -> None:
    statuses = ("pending",) if pending_only else None
    changes = agent.store.list_changes(agent.session_id, statuses=statuses)
    if not changes:
        if not pending_only:
            console.print("[text.secondary]No change proposals in this session.[/]")
        return
    table = _table("Change", "Status", "Operation", "Path", "Summary")
    for item in changes:
        table.add_row(
            Text(item.id[:12], style="text.muted"),
            _status_text(item.status),
            Text(item.operation, style="text.secondary"),
            Text(item.path, style="path"),
            Text(item.summary, style="text.secondary"),
        )
    console.print(table)
    for item in changes:
        if item.status == "pending":
            console.print(Panel(Text(item.diff or "(no textual diff)", style="code"), title=f"Review {item.id[:12]} · {item.path}", border_style="warning"))


def _approve_action(agent: WorkspaceAgent, action_id: str) -> None:
    command = _resolve_command(agent, action_id)
    if command is not None:
        _approve_command(agent, command.id)
        return
    external = _resolve_external(agent, action_id)
    if external is not None:
        _approve_external(agent, external.id)
        return
    _approve(agent, action_id)


def _approve(agent: WorkspaceAgent, change_id: str) -> None:
    if not change_id:
        console.print("[error]Error:[/] provide a change id from [command]/changes[/]")
        return
    try:
        change = _resolve_change(agent, change_id)
        console.print(Panel(Text(change.diff or "(no textual diff)", style="code"), title=f"Apply {change.id[:12]} · {change.path}", border_style="warning"))
        if console.input("Apply this change? [y/N] ").strip().casefold() not in {"y", "yes"}:
            console.print("[waiting]Change remains pending.[/]")
            return
        service = _change_service(agent)
        service.approve(change.id)
        applied = service.apply(change.id)
        console.print(f"[success]Applied:[/] [path]{applied.path}[/]. Review with [command]/diff[/]; use [command]/undo[/] to revert.")
    except (AgentRuntimeError, ValueError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def _reject(agent: WorkspaceAgent, change_id: str) -> None:
    try:
        _change_service(agent).reject(_resolve_change(agent, change_id).id)
        console.print("[warning]Discarded change proposal.[/]")
    except (ValueError, PolicyError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def _approve_command(agent: WorkspaceAgent, command_id: str) -> None:
    try:
        command = _resolve_command(agent, command_id)
        if command is None:
            raise AgentRuntimeError("command does not belong to this session or does not exist")
        console.print(Panel(Text(" ".join(command.argv), style="command"), title=f"Run {command.id[:12]} · cwd={command.cwd} · timeout={command.timeout_seconds:g}s", border_style="warning"))
        if console.input("Run this command? [y/N] ").strip().casefold() not in {"y", "yes"}:
            console.print("[waiting]Command remains pending.[/]")
            return
        service = _command_service(agent)
        service.approve(command.id)
        result = service.execute(command.id)
        console.print(f"[success]{result.status}:[/] [text.muted]exit_code={result.exit_code}[/]")
        if result.stdout:
            console.print(Panel(Text(result.stdout, style="tool"), title="stdout", border_style="border.muted"))
        if result.stderr:
            console.print(Panel(Text(result.stderr, style="error"), title="stderr", border_style="error"))
    except (AgentRuntimeError, ValueError, PolicyError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def _reject_action(agent: WorkspaceAgent, action_id: str) -> None:
    command = _resolve_command(agent, action_id)
    if command is not None:
        try:
            _command_service(agent).reject(command.id)
            console.print("[warning]Discarded command proposal.[/]")
        except (AgentRuntimeError, ValueError, PolicyError) as exc:
            console.print(f"[error]Error:[/] {exc}")
        return
    external = _resolve_external(agent, action_id)
    if external is not None:
        try:
            _service_for_external(agent, external).actions.reject(external.id)
            console.print("[warning]Discarded external action proposal.[/]")
        except (AgentRuntimeError, ValueError, PolicyError) as exc:
            console.print(f"[error]Error:[/] {exc}")
        return
    _reject(agent, action_id)


def _undo(agent: WorkspaceAgent) -> None:
    try:
        change = agent.store.last_applied_change(agent.session_id)
        if change is None:
            raise ValueError("no applied change is available to undo")
        reverse = make_diff(Path(change.path), change.after_content, change.before_content or "")
        console.print(Panel(Text(reverse or "(remove file)", style="code"), title=f"Undo {change.id[:12]} · {change.path}", border_style="warning"))
        if console.input("Undo this change? [y/N] ").strip().casefold() not in {"y", "yes"}:
            return
        undone = _change_service(agent).undo_last()
        console.print(f"[success]Undone:[/] [path]{undone.path}[/]")
    except (ValueError, PolicyError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def _show_git_diff(agent: WorkspaceAgent) -> None:
    from .tools import RunContext, workspace_tools
    context = RunContext(agent.session_id, "cli", WorkspacePolicy(agent.workspace), agent.max_turns, agent.events.emit, agent.store)
    result, _ = workspace_tools().invoke("git_diff", context, {})
    output = result.data.get("output", "") if result.ok and isinstance(result.data, dict) else result.error
    console.print(Text(str(output or ""), style="code" if result.ok else "error"))


def _render_commands(agent: WorkspaceAgent) -> None:
    commands = agent.store.list_commands(agent.session_id)
    if not commands:
        console.print("[text.secondary]No command proposals in this session.[/]")
        return
    table = _table("Command", "Status", "CWD", "Exit", "Command")
    for item in commands:
        table.add_row(
            Text(item.id[:12], style="text.muted"),
            _status_text(item.status),
            Text(item.cwd, style="path"),
            Text(str(item.exit_code) if item.exit_code is not None else "", style="text.muted"),
            Text(" ".join(item.argv), style="command"),
        )
    console.print(table)


def _render_tasks(agent: WorkspaceAgent) -> None:
    tasks = agent.store.list_tasks(agent.session_id)
    if not tasks:
        console.print("[text.secondary]No tasks in this session.[/]")
        return
    table = _table("Task", "Status", "Text")
    for item in tasks:
        table.add_row(Text(item.id[:12], style="text.muted"), _status_text(item.status), item.text)
    console.print(table)


def _render_cost(agent: WorkspaceAgent) -> None:
    input_tokens, output_tokens, cost = agent.store.session_cost(agent.session_id)
    console.print(Text.assemble(
        ("input_tokens=", "text.muted"), (f"{input_tokens}\n", "text.secondary"),
        ("output_tokens=", "text.muted"), (f"{output_tokens}\n", "text.secondary"),
        ("cost_usd=", "text.muted"), (f"${cost:.6f}", "text.secondary"),
    ))


def _permissions(agent: WorkspaceAgent, text: str) -> None:
    parts = text.split()
    if len(parts) == 1:
        choices = (
            ("full", "完全访问：不确认；保留审计与文件撤销"),
            ("approve", "为我批准：仅文件、命令、MCP 等高风险动作确认"),
            ("ask", "每次确认：每条请求及每个动作都确认"),
            ("cancel", "保持当前模式"),
        )
        default = {PermissionMode.FULL_ACCESS: 0, PermissionMode.APPROVE_FOR_ME: 1, PermissionMode.ASK_FOR_APPROVAL: 2}[agent.permission_mode]
        choice = _select_choice("选择权限模式", choices, default=default, escape_key="cancel")
        if choice == "cancel":
            console.print(f"[text.secondary]权限模式未改变：[/][text.muted]{agent.permission_mode.value}[/]")
            return
        _set_permission_mode(agent, choice)
        return
    if len(parts) != 2:
        console.print("[error]Usage:[/] [command]/permissions [full|approve|ask][/]")
        return
    _set_permission_mode(agent, parts[1])


def _set_permission_mode(agent: WorkspaceAgent, value: str) -> None:
    try:
        mode = PermissionMode.parse(value)
        agent.permission_mode = mode
        agent.store.set_workspace_setting("permission_mode", mode.value)
        console.print(f"[success]权限模式已切换：[/] {_permission_badge(mode)} [text.muted]({mode.value})[/]")
        console.print("[text.secondary]该选择已保存到当前工作区。[/]")
    except ValueError as exc:
        console.print(f"[error]Error:[/] {exc}")


def _render_external_actions(agent: WorkspaceAgent, *, kinds: set[str] | None = None) -> None:
    actions = [item for item in agent.store.list_external_actions(agent.session_id) if kinds is None or item.kind in kinds]
    if not actions:
        console.print("[text.secondary]No external action proposals in this session.[/]")
        return
    table = _table("Action", "Status", "Kind", "Summary")
    for item in actions:
        table.add_row(
            Text(item.id[:12], style="text.muted"),
            _status_text(item.status),
            Text(item.kind, style="tool"),
            Text(item.summary, style="text.secondary"),
        )
    console.print(table)


def _render_sources(agent: WorkspaceAgent) -> None:
    sources = agent.store.list_sources(agent.session_id)
    if not sources:
        console.print("[text.secondary]No external sources in this session.[/]")
        return
    table = _table("Source", "Title", "URL", "Flags")
    for item in sources:
        table.add_row(
            Text(item.id[:12], style="text.muted"),
            item.title[:60],
            Text(item.url, style="path"),
            Text("untrusted/injection" if item.suspicious else "untrusted", style="warning"),
        )
    console.print(table)


def _approve_external(agent: WorkspaceAgent, action_id: str) -> None:
    try:
        action = _resolve_external(agent, action_id)
        if action is None:
            raise AgentRuntimeError("external action does not belong to this session or does not exist")
        console.print(Panel(Text(str(_redact(action.payload)), style="tool"), title=f"Approve {action.kind} · {action.id[:12]}", border_style="warning"))
        choice = _select_choice("Run this external action?", (("approve", "Approve and run"), ("later", "Keep pending")), escape_key="later")
        if choice != "approve":
            console.print("[waiting]Action remains pending.[/]")
            return
        _execute_external(agent, action)
    except (AgentRuntimeError, ValueError, PolicyError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def _review_pending_external_actions(agent: WorkspaceAgent, run_id: str) -> list[object]:
    pending = [item for item in agent.store.list_external_actions(agent.session_id) if item.run_id == run_id and item.status == "pending"]
    completed: list[object] = []
    for action in pending:
        details = f"{action.summary}\n\nID: {action.id}\nPayload: {_redact(action.payload)}"
        console.print(Panel(Text(details, style="tool"), title=f"Approval required · {action.kind}", border_style="warning"))
        choice = _select_choice(
            "Choose an action",
            (("approve", "Approve and run"), ("reject", "Reject"), ("later", "Decide later")),
            escape_key="later",
        )
        try:
            if choice == "approve":
                result = _execute_external(agent, action)
                if result is not None and result.status == "completed":
                    completed.append(result)
            elif choice == "reject":
                _service_for_external(agent, action).actions.reject(action.id)
                console.print(f"[warning]Rejected:[/] {action.id[:12]} · {action.kind}")
            else:
                console.print(f"[waiting]Left pending:[/] [text.muted]{action.id[:12]}[/] [text.secondary]Use /approve or /reject later.[/]")
        except (AgentRuntimeError, ValueError, PolicyError) as exc:
            console.print(f"[error]Error:[/] {exc}")
    return completed


def _service_for_external(agent: WorkspaceAgent, action: object):
    return _mcp_service(agent, action.run_id) if action.kind.startswith("mcp_") else _web_service(agent, action.run_id)


def _execute_external(agent: WorkspaceAgent, action: object):
    service = _service_for_external(agent, action)
    service.actions.approve(action.id)
    result = service.execute(action.id)
    console.print(f"[success]{result.status}:[/] [text.muted]{result.id[:12]}[/] · [tool]{result.kind}[/]")
    if result.error:
        console.print(f"[error]{result.error}[/]")
    elif result.kind in {"web_search", "web_fetch"} and result.result:
        count = len(result.result.get("results", [])) if result.kind == "web_search" else 1
        console.print(f"[info]Saved {count} Web source(s);[/] [text.secondary]Agent will continue automatically.[/]")
    elif result.result:
        console.print(Panel(Text(str(_redact(result.result)), style="tool"), title="Safe result summary", border_style="border.muted"))
    return result


def _mcp_command(agent: WorkspaceAgent, text: str) -> None:
    parts = text.split()
    registry = McpRegistry(WorkspacePolicy(agent.workspace))
    try:
        if len(parts) == 1 or parts[1] == "list":
            servers = registry.servers()
            table = _table("Server", "Scope", "Enabled", "Allowed tools")
            for server in servers.values():
                table.add_row(
                    Text(server.name, style="tool"),
                    Text(server.scope, style="text.secondary"),
                    Text(str(server.enabled), style="success" if server.enabled else "text.disabled"),
                    Text(", ".join(server.allowed_tools), style="command"),
                )
            console.print(table)
        elif parts[1] in {"status", "tools"} and len(parts) == 3:
            server = registry.get(parts[2])
            console.print(Text.assemble(
                ("server=", "text.muted"), (f"{server.name}\n", "tool"),
                ("scope=", "text.muted"), (f"{server.scope}\n", "text.secondary"),
                ("cwd=", "text.muted"), (f"{server.cwd}\n", "path"),
                ("enabled=", "text.muted"), (f"{server.enabled}\n", "success" if server.enabled else "text.disabled"),
                ("allowed_tools=", "text.muted"), (", ".join(server.allowed_tools), "command"),
            ))
        else:
            console.print("[error]Usage:[/] [command]/mcp list | /mcp status <server> | /mcp tools <server>[/]")
    except (ValueError, PolicyError) as exc:
        console.print(f"[error]Error:[/] {exc}")


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {key: "<redacted>" if any(word in key.casefold() for word in ("token", "secret", "password", "key")) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _resolve_change(agent: WorkspaceAgent, prefix: str):
    exact = agent.store.get_change(prefix, session_id=agent.session_id)
    if exact is not None:
        return exact
    matches = [item for item in agent.store.list_changes(agent.session_id) if item.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise AgentRuntimeError("change does not belong to this session or does not exist")
    raise AgentRuntimeError("change id prefix is ambiguous; provide more characters")


def _resolve_command(agent: WorkspaceAgent, prefix: str):
    exact = agent.store.get_command(prefix, session_id=agent.session_id)
    if exact is not None:
        return exact
    matches = [item for item in agent.store.list_commands(agent.session_id) if item.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AgentRuntimeError("command id prefix is ambiguous; provide more characters")
    return None


def _resolve_external(agent: WorkspaceAgent, prefix: str):
    exact = agent.store.get_external_action(prefix, session_id=agent.session_id)
    if exact is not None:
        return exact
    matches = [item for item in agent.store.list_external_actions(agent.session_id) if item.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AgentRuntimeError("external action id prefix is ambiguous; provide more characters")
    return None


def _sessions(store: SessionStore, limit: int) -> int:
    table = _table("Session", "Workspace", "Model", "Updated")
    for item in store.list(limit):
        table.add_row(
            Text(item.id, style="text.muted"),
            Text(str(item.workspace), style="path"),
            Text(item.model, style="text.secondary"),
            Text(item.updated_at, style="text.muted"),
        )
    console.print(table)
    return 0


def _doctor(workspace: Path, settings: Settings) -> int:
    table = _table("Check", "Result")
    table.add_row("Workspace", Text(str(workspace), style="path"))
    table.add_row("SQLite", Text(str(workspace / ".capslock" / "capslock.sqlite3"), style="path"))
    table.add_row("Model", Text(settings.model, style="text.secondary"))
    table.add_row("Endpoint", Text(settings.base_url, style="path"))
    api_ready = bool(settings.api_key and not settings.api_key.startswith("your_"))
    table.add_row("API key", Text("configured" if api_ready else "missing", style="success" if api_ready else "warning"))
    git_ready = (workspace / ".git").exists()
    table.add_row("Git", Text("repository" if git_ready else "not a repository (Git tools will be unavailable)", style="success" if git_ready else "warning"))
    commands = CommandService(_store(workspace), WorkspacePolicy(workspace), "doctor", "doctor", lambda *args, **kwargs: None)
    table.add_row("Command templates", Text(", ".join(commands.available_templates()) or "none detected", style="command"))
    console.print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
