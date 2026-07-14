"""Interactive and one-shot CLI for the read-only workspace agent."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.table import Table

from .config import Settings
from .environment import load_project_environment
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
    return WorkspaceAgent(_client(settings), workspace=workspace, model=settings.model, store=_store(workspace), session_id=session_id, max_turns=settings.max_turns, max_context_messages=settings.max_context_messages)


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
    console.print("Use /help for commands. The agent is read-only.")
    while True:
        try:
            question = console.input("\n[bold cyan]You>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if question in {"/exit", "/quit"}:
            return 0
        if question == "/help":
            console.print("/status  /context  /session  /clear  /cancel  /exit")
            continue
        if question in {"/status", "/session"}:
            console.print(f"session={agent.session_id}\nworkspace={agent.workspace}\nmodel={agent.model}\nmax_turns={agent.max_turns}")
            continue
        if question == "/context":
            console.print(f"Stored context messages: {len(agent.store.messages(agent.session_id, agent.max_context_messages))}/{agent.max_context_messages}")
            continue
        if question == "/clear":
            console.print("This session is append-only. Start `capslock chat` to create a fresh session.")
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
    console.print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
