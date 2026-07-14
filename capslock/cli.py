"""Interactive and one-shot CLI for the read-only workspace agent."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .changes import ChangeService, make_diff
from .execution import CommandService
from .config import Settings
from .environment import load_project_environment
from .policy import PolicyError, WorkspacePolicy
from .runtime import AgentRuntimeError, WorkspaceAgent
from .session import SessionStore


console = Console()


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
    return WorkspaceAgent(_client(settings), workspace=workspace, model=settings.model, store=_store(workspace), session_id=session_id, max_turns=settings.max_turns, max_context_messages=settings.max_context_messages, command_timeout_seconds=settings.command_timeout_seconds, command_output_bytes=settings.command_output_bytes, input_cost_per_million=settings.input_cost_per_million, output_cost_per_million=settings.output_cost_per_million)


def main(argv: list[str] | None = None) -> int:
    load_project_environment()
    args = build_parser().parse_args(argv)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        console.print(f"[red]Error:[/] workspace does not exist: {workspace}")
        return 2
    settings = Settings.load(workspace)
    try:
        if args.command == "doctor":
            return _doctor(workspace, settings)
        if args.command == "sessions":
            return _sessions(_store(workspace), args.limit)
        agent = _agent(workspace, settings, getattr(args, "session_id", None))
        if args.command == "ask":
            with console.status("[bold green]Agent is analyzing the workspace...[/]"):
                answer = agent.ask(args.question)
            _render(answer, args.debug)
            return 0
        return _chat(agent, args.debug)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")
        return 130
    except AgentRuntimeError as exc:
        console.print(f"[red]Error:[/] {exc}")
        return 2
    except Exception as exc:
        console.print(f"[red]Model or transport error:[/] {exc}")
        return 1


def _chat(agent: WorkspaceAgent, debug: bool) -> int:
    console.print(f"[bold green]Agent session started[/]  workspace={agent.workspace}  session={agent.session_id[:12]}")
    console.print("Use /help for commands. File changes are proposed, reviewed, and approved one at a time.")
    while True:
        try:
            question = console.input("\n[bold cyan]You>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if question in {"/exit", "/quit"}:
            return 0
        if question == "/help":
            console.print("/status  /context  /cost  /tasks  /changes  /commands  /approve <id>  /reject <id>  /undo  /diff  /clear  /cancel  /exit")
            continue
        if question in {"/status", "/session"}:
            console.print(f"session={agent.session_id}\nworkspace={agent.workspace}\nmodel={agent.model}\nmax_turns={agent.max_turns}")
            continue
        if question == "/context":
            console.print(f"Stored context messages: {len(agent.store.messages(agent.session_id, agent.max_context_messages))}/{agent.max_context_messages}")
            continue
        if question == "/cost":
            _render_cost(agent)
            continue
        if question == "/tasks":
            _render_tasks(agent)
            continue
        if question == "/clear":
            console.print("This session is append-only. Start `capslock chat` to create a fresh session.")
            continue
        if question == "/changes":
            _render_changes(agent)
            continue
        if question == "/commands":
            _render_commands(agent)
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
            console.print("No background run is active. Press Ctrl-C while a request is running to cancel it.")
            continue
        if not question:
            continue
        try:
            with console.status("[bold green]Agent is analyzing the workspace...[/]"):
                answer = agent.ask(question)
            _render(answer, debug)
            _render_changes(agent, pending_only=True)
        except AgentRuntimeError as exc:
            console.print(f"[red]Error:[/] {exc}")


def _render(answer: object, debug: bool) -> None:
    from .runtime import WorkspaceAnswer
    assert isinstance(answer, WorkspaceAnswer)
    console.print(f"\n[bold green]Agent>[/] {answer.text}")
    if answer.citations:
        for item in answer.citations:
            console.print(f"  [dim]Evidence:[/] {item.path}:L{item.start_line}-L{item.end_line} ({item.id})")
    console.print(f"  [dim]Run {answer.run_id[:8]} · {answer.duration_ms}ms[/]")
    if debug:
        for event in answer.events:
            console.print(f"  [dim]{event.kind}: {event.data}[/]")


def _change_service(agent: WorkspaceAgent) -> ChangeService:
    return ChangeService(agent.store, WorkspacePolicy(agent.workspace), agent.session_id, "cli", agent.events.emit)


def _command_service(agent: WorkspaceAgent) -> CommandService:
    return CommandService(agent.store, WorkspacePolicy(agent.workspace), agent.session_id, "cli", agent.events.emit, timeout_seconds=agent.command_timeout_seconds, output_limit_bytes=agent.command_output_bytes)


def _render_changes(agent: WorkspaceAgent, *, pending_only: bool = False) -> None:
    statuses = ("pending",) if pending_only else None
    changes = agent.store.list_changes(agent.session_id, statuses=statuses)
    if not changes:
        if not pending_only:
            console.print("[dim]No change proposals in this session.[/]")
        return
    table = Table("Change", "Status", "Operation", "Path", "Summary")
    for item in changes:
        table.add_row(item.id[:12], item.status, item.operation, item.path, item.summary)
    console.print(table)
    for item in changes:
        if item.status == "pending":
            console.print(Panel(item.diff or "(no textual diff)", title=f"Review {item.id[:12]} · {item.path}", border_style="yellow"))


def _approve_action(agent: WorkspaceAgent, action_id: str) -> None:
    command = _resolve_command(agent, action_id)
    if command is not None:
        _approve_command(agent, command.id)
        return
    _approve(agent, action_id)


def _approve(agent: WorkspaceAgent, change_id: str) -> None:
    if not change_id:
        console.print("[red]Error:[/] provide a change id from /changes")
        return
    try:
        change = _resolve_change(agent, change_id)
        console.print(Panel(change.diff or "(no textual diff)", title=f"Apply {change.id[:12]} · {change.path}", border_style="yellow"))
        if console.input("Apply this change? [y/N] ").strip().casefold() not in {"y", "yes"}:
            console.print("[dim]Change remains pending.[/]")
            return
        service = _change_service(agent)
        service.approve(change.id)
        applied = service.apply(change.id)
        console.print(f"[green]Applied:[/] {applied.path}. Review with /diff; use /undo to revert.")
    except (AgentRuntimeError, ValueError) as exc:
        console.print(f"[red]Error:[/] {exc}")


def _reject(agent: WorkspaceAgent, change_id: str) -> None:
    try:
        _change_service(agent).reject(_resolve_change(agent, change_id).id)
        console.print("[yellow]Discarded change proposal.[/]")
    except (ValueError, PolicyError) as exc:
        console.print(f"[red]Error:[/] {exc}")


def _approve_command(agent: WorkspaceAgent, command_id: str) -> None:
    try:
        command = _resolve_command(agent, command_id)
        if command is None:
            raise AgentRuntimeError("command does not belong to this session or does not exist")
        console.print(Panel(" ".join(command.argv), title=f"Run {command.id[:12]} · cwd={command.cwd} · timeout={command.timeout_seconds:g}s", border_style="yellow"))
        if console.input("Run this command? [y/N] ").strip().casefold() not in {"y", "yes"}:
            console.print("[dim]Command remains pending.[/]")
            return
        service = _command_service(agent)
        service.approve(command.id)
        result = service.execute(command.id)
        console.print(f"[green]{result.status}:[/] exit_code={result.exit_code}")
        if result.stdout:
            console.print(Panel(result.stdout, title="stdout", border_style="dim"))
        if result.stderr:
            console.print(Panel(result.stderr, title="stderr", border_style="red"))
    except (AgentRuntimeError, ValueError, PolicyError) as exc:
        console.print(f"[red]Error:[/] {exc}")


def _reject_action(agent: WorkspaceAgent, action_id: str) -> None:
    command = _resolve_command(agent, action_id)
    if command is not None:
        try:
            _command_service(agent).reject(command.id)
            console.print("[yellow]Discarded command proposal.[/]")
        except (AgentRuntimeError, ValueError, PolicyError) as exc:
            console.print(f"[red]Error:[/] {exc}")
        return
    _reject(agent, action_id)


def _undo(agent: WorkspaceAgent) -> None:
    try:
        change = agent.store.last_applied_change(agent.session_id)
        if change is None:
            raise ValueError("no applied change is available to undo")
        reverse = make_diff(Path(change.path), change.after_content, change.before_content or "")
        console.print(Panel(reverse or "(remove file)", title=f"Undo {change.id[:12]} · {change.path}", border_style="yellow"))
        if console.input("Undo this change? [y/N] ").strip().casefold() not in {"y", "yes"}:
            return
        undone = _change_service(agent).undo_last()
        console.print(f"[green]Undone:[/] {undone.path}")
    except (ValueError, PolicyError) as exc:
        console.print(f"[red]Error:[/] {exc}")


def _show_git_diff(agent: WorkspaceAgent) -> None:
    from .tools import RunContext, workspace_tools
    context = RunContext(agent.session_id, "cli", WorkspacePolicy(agent.workspace), agent.max_turns, agent.events.emit, agent.store)
    result, _ = workspace_tools().invoke("git_diff", context, {})
    console.print(result.data.get("output", "") if result.ok and isinstance(result.data, dict) else result.error)


def _render_commands(agent: WorkspaceAgent) -> None:
    commands = agent.store.list_commands(agent.session_id)
    if not commands:
        console.print("[dim]No command proposals in this session.[/]")
        return
    table = Table("Command", "Status", "CWD", "Exit", "Command")
    for item in commands:
        table.add_row(item.id[:12], item.status, item.cwd, str(item.exit_code) if item.exit_code is not None else "", " ".join(item.argv))
    console.print(table)


def _render_tasks(agent: WorkspaceAgent) -> None:
    tasks = agent.store.list_tasks(agent.session_id)
    if not tasks:
        console.print("[dim]No tasks in this session.[/]")
        return
    table = Table("Task", "Status", "Text")
    for item in tasks:
        table.add_row(item.id[:12], item.status, item.text)
    console.print(table)


def _render_cost(agent: WorkspaceAgent) -> None:
    input_tokens, output_tokens, cost = agent.store.session_cost(agent.session_id)
    console.print(f"input_tokens={input_tokens}\noutput_tokens={output_tokens}\ncost_usd=${cost:.6f}")


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


def _sessions(store: SessionStore, limit: int) -> int:
    table = Table("Session", "Workspace", "Model", "Updated")
    for item in store.list(limit):
        table.add_row(item.id, str(item.workspace), item.model, item.updated_at)
    console.print(table)
    return 0


def _doctor(workspace: Path, settings: Settings) -> int:
    table = Table("Check", "Result")
    table.add_row("Workspace", str(workspace))
    table.add_row("SQLite", str(workspace / ".capslock" / "capslock.sqlite3"))
    table.add_row("Model", settings.model)
    table.add_row("Endpoint", settings.base_url)
    table.add_row("API key", "configured" if settings.api_key and not settings.api_key.startswith("your_") else "missing")
    table.add_row("Git", "repository" if (workspace / ".git").exists() else "not a repository (Git tools will be unavailable)")
    commands = CommandService(_store(workspace), WorkspacePolicy(workspace), "doctor", "doctor", lambda *args, **kwargs: None)
    table.add_row("Command templates", ", ".join(commands.available_templates()) or "none detected")
    console.print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
