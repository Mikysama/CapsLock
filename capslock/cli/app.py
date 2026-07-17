"""Composition root for the CapsLock command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from openai import OpenAI
from rich.console import Console

from .. import __version__
from ..application.app import WorkspaceApplication
from ..config import Settings
from ..environment import load_project_environment
from ..layout import LayoutConflict, ProjectLayout
from ..runtime import AgentRuntimeError
from ..theme import make_console
from ..storage import MemoryStore
from ..session_management import SessionManager
from .chat import run_chat
from .context import CliContext
from .diagnostics import (
    archive_saved_session,
    delete_saved_session,
    doctor,
    export_saved_session,
    open_store,
    rename_saved_session,
    render_sessions,
    search_saved_sessions,
    select_saved_session,
)
from .exec import run_exec
from .migration import run_layout_migration
from .tui import run_tui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capslock", description="Lock in your focus. Unlock your potential.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Show runtime events after each answer")
    subparsers = parser.add_subparsers(dest="command")
    parser.set_defaults(command="chat")
    chat = subparsers.add_parser("chat", help="Start a new interactive session")
    chat.add_argument("--classic", action="store_true", help="Use the v1.6 line-oriented interface")
    ask = subparsers.add_parser("ask", help="Ask one question and exit")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true", help="Emit versioned JSONL workflow events")
    execute = subparsers.add_parser("exec", help="Run one non-interactive request")
    execute.add_argument("question", nargs="?", help="Prompt; read from stdin when omitted")
    execute.add_argument("--json", action="store_true", help="Emit versioned JSONL workflow events")
    resume = subparsers.add_parser("resume", help="Resume a saved interactive session")
    resume.add_argument("session_id", nargs="?", help="Full session ID or a unique prefix")
    resume.add_argument("--limit", type=int, default=20, help="Maximum sessions shown in the selector")
    sessions = subparsers.add_parser("sessions", help="List saved sessions")
    sessions.add_argument("--limit", type=int, default=20)
    session_commands = sessions.add_subparsers(dest="sessions_command")
    rename = session_commands.add_parser("rename", help="Rename a saved session")
    rename.add_argument("session_id", help="Full session ID or a unique prefix")
    rename.add_argument("title", nargs="+", help="New session title")
    search = session_commands.add_parser("search", help="Search session titles and messages")
    search.add_argument("query", nargs="+", help="Full-text search query")
    search.add_argument("--archived", action="store_true", help="Include archived sessions")
    archive = session_commands.add_parser("archive", help="Archive a saved session")
    archive.add_argument("session_id")
    unarchive = session_commands.add_parser("unarchive", help="Restore an archived session")
    unarchive.add_argument("session_id")
    export = session_commands.add_parser("export", help="Export a session as JSON and Markdown")
    export.add_argument("session_id")
    export.add_argument("destination")
    delete = session_commands.add_parser("delete", help="Permanently delete a session")
    delete.add_argument("session_id")
    delete.add_argument("--yes", action="store_true")
    subparsers.add_parser("doctor", help="Check local configuration and workspace access")
    migrate = subparsers.add_parser("migrate-layout", help="Preview or apply the .capslock layout migration")
    migrate.add_argument("--scope", choices=("workspace", "user", "all"), default="workspace")
    mode = migrate.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="Preview without changing files (default)")
    mode.add_argument("--apply", dest="apply", action="store_true", help="Copy, verify, and remove legacy sources")
    migrate.set_defaults(apply=False)
    migrate.add_argument("--yes", action="store_true", help="Confirm apply without an interactive prompt")
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
    *,
    layout: ProjectLayout | None = None,
) -> WorkspaceApplication:
    return WorkspaceApplication(
        workspace=workspace,
        settings=settings,
        client=create_client(settings),
        session_id=session_id,
        layout=layout,
    )


def main(argv: list[str] | None = None, *, console: Console | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = console or make_console()
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        output.print(f"[error]Error:[/] [path]{workspace}[/]")
        return 2
    try:
        layout = ProjectLayout.discover(workspace)
        if args.command == "migrate-layout":
            return run_layout_migration(
                output,
                layout,
                scope=args.scope,
                apply=args.apply,
                yes=args.yes,
            )
        load_project_environment(workspace)
        settings = Settings.load(workspace, layout=layout)
        for warning in layout.warnings:
            output.print(f"[warning]Warning:[/] {warning}")
        if args.command == "doctor":
            return doctor(output, workspace, settings, layout=layout)
        if args.command == "sessions":
            with open_store(workspace, layout=layout) as store:
                if args.sessions_command == "rename":
                    return rename_saved_session(output, store, args.session_id, " ".join(args.title))
                if args.sessions_command == "search":
                    return search_saved_sessions(output, store, " ".join(args.query), args.limit, include_archived=args.archived)
                if args.sessions_command in {"archive", "unarchive"}:
                    return archive_saved_session(output, store, args.session_id, archived=args.sessions_command == "archive")
                if args.sessions_command in {"export", "delete"}:
                    memory_path = settings.memory.database or layout.user.memory
                    with MemoryStore(memory_path) as memory:
                        manager = SessionManager(store, workspace=workspace, memory_store=memory)
                        if args.sessions_command == "export":
                            return export_saved_session(output, manager, store, args.session_id, args.destination)
                        return delete_saved_session(output, manager, store, args.session_id, yes=args.yes)
                return render_sessions(output, store, args.limit)
        session_id = getattr(args, "session_id", None)
        if args.command == "resume":
            with open_store(workspace, layout=layout) as store:
                if session_id is None:
                    session_id = select_saved_session(output, store, args.limit)
                    if session_id is None:
                        return 0
                else:
                    session = store.resolve_session(session_id)
                    if session is None:
                        raise AgentRuntimeError(f"session does not exist: {session_id}")
                    session_id = session.id
        with create_application(workspace, settings, session_id, layout=layout) as application:
            context = CliContext(output, application.agent)
            if args.command in {"ask", "exec"}:
                return run_exec(context, args.question, json_events=args.json)
            use_tui = (
                not getattr(args, "classic", False)
                and sys.stdin.isatty()
                and output.is_terminal
                and os.environ.get("TERM", "").casefold() != "dumb"
            )
            return run_tui(context, args.debug) if use_tui else run_chat(context, args.debug)
    except KeyboardInterrupt:
        output.print("\n[warning]Cancelled.[/]")
        return 130
    except LayoutConflict as exc:
        output.print(f"[error]Layout conflict:[/] {exc}")
        return 2
    except (AgentRuntimeError, ValueError) as exc:
        output.print(f"[error]Error:[/] {exc}")
        return 2
    except Exception as exc:
        output.print(f"[error]Model or transport error:[/] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
