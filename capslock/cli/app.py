"""CapsLock v2 command-line composition root."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from openai import AsyncOpenAI
from rich.console import Console

from .. import __version__
from ..application.app import WorkspaceApplication
from ..config import Settings
from ..environment import load_project_environment
from ..layout import LayoutConflict, ProjectLayout
from ..runtime import AgentRuntimeError
from ..session_management import SessionManager
from ..storage.async_database import IncompatibleDatabaseError
from ..storage.memory_v2 import MemoryRepositories
from ..storage.repositories_v2 import WorkspaceRepositories
from ..theme import make_console
from .context import CliContext
from .diagnostics import (
    archive_session,
    delete_session,
    doctor,
    export_session,
    list_sessions,
    rename_session,
    search_sessions,
    select_session,
)
from .exec import run_exec
from .status import dynamic_status_supported
from .tui import run_tui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capslock", description="A local, recoverable workspace agent"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--no-spinner", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    execute = subparsers.add_parser("exec", help="Run one non-interactive request")
    execute.add_argument("question", nargs="?")
    execute.add_argument("--json", action="store_true")
    execute.add_argument("--no-spinner", action="store_true", default=argparse.SUPPRESS)
    execute.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    resume = subparsers.add_parser("resume", help="Resume a saved TUI session")
    resume.add_argument("session_id", nargs="?")
    resume.add_argument("--limit", type=int, default=20)
    resume.add_argument("--no-spinner", action="store_true", default=argparse.SUPPRESS)
    resume.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    sessions = subparsers.add_parser(
        "sessions", aliases=["session"], help="Manage saved sessions"
    )
    sessions.add_argument("--limit", type=int, default=20)
    commands = sessions.add_subparsers(dest="sessions_command")
    rename = commands.add_parser("rename")
    rename.add_argument("session_id")
    rename.add_argument("title", nargs="+")
    search = commands.add_parser("search")
    search.add_argument("query", nargs="+")
    search.add_argument("--archived", action="store_true")
    archive = commands.add_parser("archive")
    archive.add_argument("session_id")
    unarchive = commands.add_parser("unarchive")
    unarchive.add_argument("session_id")
    export = commands.add_parser("export")
    export.add_argument("session_id")
    export.add_argument("destination")
    delete = commands.add_parser("delete")
    delete.add_argument("session_id", nargs="?")
    delete.add_argument("--yes", action="store_true")
    subparsers.add_parser("doctor", help="Check v2 configuration and state")
    return parser


def create_client(settings: Settings) -> AsyncOpenAI:
    if not settings.model_config.api_key or settings.model_config.api_key.startswith(
        "your_"
    ):
        raise AgentRuntimeError("API key is not configured")
    return AsyncOpenAI(
        api_key=settings.model_config.api_key,
        base_url=settings.model_config.base_url,
        timeout=settings.model_config.timeout_seconds,
    )


def create_provider_clients(settings: Settings) -> dict[str, AsyncOpenAI]:
    clients: dict[str, AsyncOpenAI] = {}
    for name, provider in (settings.providers or {}).items():
        if not provider.api_key or provider.api_key.startswith("your_"):
            continue
        clients[name] = AsyncOpenAI(
            api_key=provider.api_key,
            base_url=provider.base_url,
            timeout=provider.timeout_seconds,
        )
    if not clients:
        # Preserve the existing actionable error for the implicit legacy profile.
        create_client(settings)
    return clients


async def create_application(
    workspace: Path,
    settings: Settings,
    session_id: str | None = None,
    *,
    layout: ProjectLayout | None = None,
) -> WorkspaceApplication:
    return await WorkspaceApplication.open(
        workspace=workspace,
        settings=settings,
        client=create_provider_clients(settings),
        session_id=session_id,
        layout=layout,
    )


def main(argv: list[str] | None = None, *, console: Console | None = None) -> int:
    try:
        return asyncio.run(async_main(argv, console=console))
    except KeyboardInterrupt:
        # The active renderer has already restored the cursor and reported cancellation.
        return 130


async def async_main(
    argv: list[str] | None = None, *, console: Console | None = None
) -> int:
    args = build_parser().parse_args(argv)
    output = console or make_console()
    errors = (
        make_console(file=sys.stderr)
        if console is None and args.command == "exec"
        else output
    )
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        output.print(f"[error]Error:[/] workspace is not a directory: {workspace}")
        return 2
    try:
        layout = ProjectLayout.discover(workspace)
        load_project_environment(workspace)
        settings = Settings.load(workspace, layout=layout)
        if args.command == "doctor":
            return await doctor(output, workspace, settings, layout=layout)
        if args.command in {"session", "sessions"}:
            return await _sessions(output, args, workspace, layout, settings)
        session_id = getattr(args, "session_id", None)
        if args.command == "resume":
            repositories = await WorkspaceRepositories.open(
                layout.database, workspace=workspace
            )
            try:
                if session_id is None:
                    session_id = await select_session(output, repositories, args.limit)
                    if session_id is None:
                        return 0
                else:
                    session = await repositories.sessions.resolve(session_id)
                    if session is None:
                        raise AgentRuntimeError(f"session does not exist: {session_id}")
                    session_id = session.id
            finally:
                await repositories.close()
        if args.command is None and not (
            sys.stdin.isatty()
            and output.is_terminal
            and os.environ.get("TERM", "").casefold() != "dumb"
        ):
            output.print(
                "[error]Interactive TUI requires a terminal; use `capslock exec`.[/]"
            )
            return 2
        application = await create_application(
            workspace, settings, session_id, layout=layout
        )
        async with application:
            context = CliContext(output, application.agent)
            if args.command == "exec":
                return await run_exec(
                    context,
                    args.question,
                    json_events=args.json,
                    spinner=not args.no_spinner,
                    quiet=args.quiet,
                )
            return await run_tui(
                context,
                status_enabled=not args.no_spinner
                and not args.quiet
                and dynamic_status_supported(
                    output.file, output_is_tty=output.is_terminal
                ),
            )
    except KeyboardInterrupt:
        errors.print("\n[warning]Cancelled.[/]")
        return 130
    except (LayoutConflict, IncompatibleDatabaseError) as exc:
        errors.print(f"[error]Incompatible state:[/] {exc}")
        return 2
    except (AgentRuntimeError, ValueError) as exc:
        errors.print(f"[error]Error:[/] {exc}")
        return 2
    except Exception as exc:
        errors.print(f"[error]Model or transport error:[/] {exc}")
        return 1


async def _sessions(
    output: Console, args, workspace: Path, layout: ProjectLayout, settings: Settings
) -> int:
    repositories = await WorkspaceRepositories.open(
        layout.database, workspace=workspace
    )
    memory_repositories = None
    try:
        command = args.sessions_command
        if command == "rename":
            return await rename_session(
                output, repositories, args.session_id, " ".join(args.title)
            )
        if command == "search":
            return await search_sessions(
                output,
                repositories,
                " ".join(args.query),
                args.limit,
                include_archived=args.archived,
            )
        if command in {"archive", "unarchive"}:
            return await archive_session(
                output, repositories, args.session_id, archived=command == "archive"
            )
        if command in {"export", "delete"}:
            memory_repositories = await MemoryRepositories.open(
                settings.memory.database or layout.user.canonical_memory
            )
            manager = SessionManager(
                repositories,
                workspace=workspace,
                memory_repositories=memory_repositories,
            )
            if command == "export":
                return await export_session(
                    output, manager, repositories, args.session_id, args.destination
                )
            return await delete_session(
                output,
                manager,
                repositories,
                args.session_id,
                yes=args.yes,
                limit=args.limit,
            )
        return await list_sessions(output, repositories, args.limit)
    finally:
        if memory_repositories is not None:
            await memory_repositories.close()
        await repositories.close()


if __name__ == "__main__":
    raise SystemExit(main())
