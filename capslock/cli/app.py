"""Composition root for the CapsLock command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path

from openai import OpenAI
from rich.console import Console

from .. import __version__
from ..application.app import WorkspaceApplication
from ..config import Settings
from ..environment import load_project_environment
from ..runtime import AgentRuntimeError
from ..theme import make_console
from .chat import run_chat
from .context import CliContext
from .diagnostics import doctor, open_store, render_sessions
from .render import render_answer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capslock", description="Trustworthy read-only workspace agent.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root (default: current directory)",
    )
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


def create_client(settings: Settings) -> OpenAI:
    if not settings.api_key or settings.api_key.startswith("your_"):
        raise AgentRuntimeError(
            "API key is not set; use CAPSLOCK_API_KEY or DEEPSEEK_API_KEY in your environment or .env"
        )
    return OpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=settings.timeout_seconds,
    )


def create_application(
    workspace: Path,
    settings: Settings,
    session_id: str | None = None,
) -> WorkspaceApplication:
    return WorkspaceApplication(
        workspace=workspace,
        settings=settings,
        client=create_client(settings),
        session_id=session_id,
    )


def main(argv: list[str] | None = None, *, console: Console | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = console or make_console()
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        output.print(f"[error]Error:[/] [path]{workspace}[/]")
        return 2
    load_project_environment(workspace)
    settings = Settings.load(workspace)
    try:
        if args.command == "doctor":
            return doctor(output, workspace, settings)
        if args.command == "sessions":
            with open_store(workspace) as store:
                return render_sessions(output, store, args.limit)
        with create_application(workspace, settings, getattr(args, "session_id", None)) as application:
            context = CliContext(output, application.agent)
            if args.command == "ask":
                with output.status("[running.bold]Agent is analyzing the workspace...[/]"):
                    answer = application.agent.ask(args.question)
                render_answer(output, answer, args.debug)
                return 0
            return run_chat(context, args.debug)
    except KeyboardInterrupt:
        output.print("\n[warning]Cancelled.[/]")
        return 130
    except AgentRuntimeError as exc:
        output.print(f"[error]Error:[/] {exc}")
        return 2
    except Exception as exc:
        output.print(f"[error]Model or transport error:[/] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
