"""CapsLock v2 command-line composition root."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from rich.console import Console

from .. import __version__
from ..bootstrap import WorkspaceApplication
from ..configuration import Settings
from ..credentials import CredentialError
from ..environment import load_project_environment
from ..layout import LayoutConflict, ProjectLayout
from ..lifecycle import LifecycleError
from ..runtime import AgentRuntimeError
from ..domain import RunLimits
from ..session_management import SessionManager
from ..storage.async_database import IncompatibleDatabaseError
from ..storage.memory_repositories import MemoryRepositories
from ..storage.repositories import WorkspaceRepositories
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
from .lifecycle import backup_command, export_lifecycle, import_lifecycle
from .plugins import plugin_command
from .providers import create_provider_clients
from .status import dynamic_status_supported
from .setup import (
    config_migrate,
    config_validate,
    credentials_command,
    initialize,
)
from .tui import run_tui
from .fullscreen_tui import run_fullscreen_tui, select_session_fullscreen


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
    parser.add_argument("--ui", choices=("inline", "fullscreen"))
    execute = subparsers.add_parser("exec", help="Run one non-interactive request")
    execute.add_argument("question", nargs="?")
    execute.add_argument("--json", action="store_true")
    execute.add_argument("--no-spinner", action="store_true", default=argparse.SUPPRESS)
    execute.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    execute.add_argument("--max-tool-rounds", type=_positive_int)
    execute.add_argument("--max-tool-calls", type=_positive_int)
    execute.add_argument("--max-duration-seconds", type=_positive_float)
    execute.add_argument("--max-tokens", type=_positive_int)
    execute.add_argument("--max-budget-usd", type=_positive_float)
    resume = subparsers.add_parser("resume", help="Resume a saved TUI session")
    resume.add_argument("session_id", nargs="?")
    resume.add_argument("--limit", type=int, default=20)
    resume.add_argument("--no-spinner", action="store_true", default=argparse.SUPPRESS)
    resume.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    resume.add_argument(
        "--ui",
        choices=("inline", "fullscreen"),
        default=argparse.SUPPRESS,
    )
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
    initialize_parser = subparsers.add_parser("init", help="Initialize a workspace")
    initialize_parser.add_argument("--non-interactive", action="store_true")
    initialize_parser.add_argument("--provider")
    initialize_parser.add_argument("--base-url")
    initialize_parser.add_argument("--model")
    initialize_parser.add_argument("--credential")
    initialize_parser.add_argument("--tavily-credential")
    initialize_parser.add_argument("--permission-mode")
    initialize_parser.add_argument("--disable-memory", action="store_true")
    initialize_parser.add_argument("--update", action="store_true")
    initialize_parser.add_argument("--check-provider", action="store_true")
    config_parser = subparsers.add_parser(
        "config", help="Validate or migrate configuration"
    )
    config_commands = config_parser.add_subparsers(dest="config_command")
    validate = config_commands.add_parser("validate")
    validate.add_argument("--json", action="store_true")
    validate.add_argument("--strict", action="store_true")
    migrate = config_commands.add_parser("migrate")
    migrate_mode = migrate.add_mutually_exclusive_group()
    migrate_mode.add_argument("--dry-run", action="store_true")
    migrate_mode.add_argument("--apply", action="store_true")
    credentials = subparsers.add_parser("credentials", help="Manage OS credentials")
    credential_commands = credentials.add_subparsers(dest="credentials_command")
    credential_commands.add_parser("status")
    credential_set = credential_commands.add_parser("set")
    credential_set.add_argument("name")
    credential_set.add_argument("--stdin", action="store_true")
    credential_delete = credential_commands.add_parser("delete")
    credential_delete.add_argument("name")
    credential_delete.add_argument("--yes", action="store_true")
    backup = subparsers.add_parser("backup", help="Create or restore local backups")
    backup_commands = backup.add_subparsers(dest="backup_command")
    backup_create = backup_commands.add_parser("create")
    backup_create.add_argument("destination", nargs="?", type=Path)
    backup_commands.add_parser("list")
    backup_verify = backup_commands.add_parser("verify")
    backup_verify.add_argument("archive", type=Path)
    backup_restore = backup_commands.add_parser("restore")
    backup_restore.add_argument("archive", type=Path)
    backup_restore.add_argument("--yes", action="store_true")
    portable_export = subparsers.add_parser(
        "export", help="Create a portable data export"
    )
    portable_export.add_argument("destination", type=Path)
    portable_export.add_argument("--include-global-memory", action="store_true")
    portable_import = subparsers.add_parser(
        "import", help="Merge a portable data export"
    )
    portable_import.add_argument("archive", type=Path)
    portable_import.add_argument("--yes", action="store_true")
    plugins = subparsers.add_parser(
        "plugin", aliases=["plugins"], help="Manage local tool plugins"
    )
    plugin_commands = plugins.add_subparsers(dest="plugin_command")
    plugin_commands.add_parser("list")
    plugin_show = plugin_commands.add_parser("show")
    plugin_show.add_argument("name")
    plugin_verify = plugin_commands.add_parser("verify")
    plugin_verify.add_argument("name")
    for command in ("install", "upgrade"):
        plugin_install = plugin_commands.add_parser(command)
        plugin_install.add_argument("path", type=Path)
        plugin_install.add_argument("--yes", action="store_true")
    for command in ("enable", "disable", "uninstall"):
        plugin_change = plugin_commands.add_parser(command)
        plugin_change.add_argument("name")
        plugin_change.add_argument("--yes", action="store_true")
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check configuration and state"
    )
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--strict", action="store_true")
    doctor_parser.add_argument("--network", action="store_true")
    doctor_parser.add_argument("--fix", action="store_true")
    doctor_parser.add_argument("--yes", action="store_true")
    return parser


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


def create_client(settings: Settings):
    """Compatibility export; provider construction lives in ``cli.providers``."""

    from .providers import create_client as provider_client

    return provider_client(settings)


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
        if args.command == "init":
            return await initialize(output, workspace, args)
        if args.command == "config":
            if args.config_command in {None, "validate"}:
                return await config_validate(
                    output,
                    layout.config,
                    json_output=getattr(args, "json", False),
                    strict=getattr(args, "strict", False),
                )
            return await config_migrate(output, layout.config, apply=bool(args.apply))
        if args.command == "credentials":
            return await credentials_command(output, layout.config, args)
        if args.command in {"plugin", "plugins"}:
            return await plugin_command(output, layout, args)
        if args.command == "doctor":
            return await doctor(output, workspace, layout=layout, args=args)
        if args.command == "backup":
            return await backup_command(output, layout, args)
        if args.command == "export":
            return await export_lifecycle(
                output,
                layout,
                args.destination,
                include_global_memory=args.include_global_memory,
            )
        if args.command == "import":
            journal = layout.root / "state" / "lifecycle-journal.json"
            if journal.exists():
                raise LifecycleError(
                    f"an incomplete lifecycle operation requires `capslock doctor --fix`: {journal}"
                )
            return await import_lifecycle(output, layout, args.archive, yes=args.yes)
        journal = layout.root / "state" / "lifecycle-journal.json"
        if journal.exists():
            raise LifecycleError(
                f"an incomplete lifecycle operation requires `capslock doctor --fix`: {journal}"
            )
        settings = Settings.load(workspace, layout=layout)
        if args.command in {"session", "sessions"}:
            return await _sessions(output, args, workspace, layout, settings)
        session_id = getattr(args, "session_id", None)
        ui_mode = _ui_mode(args) if args.command in {None, "resume"} else "inline"
        interactive_terminal = (
            sys.stdin.isatty()
            and output.is_terminal
            and os.environ.get("TERM", "").casefold() != "dumb"
        )
        if args.command in {None, "resume"} and not interactive_terminal:
            output.print(
                "[error]Interactive TUI requires a terminal; use `capslock exec`.[/]"
            )
            return 2
        if args.command == "resume":
            repositories = await WorkspaceRepositories.open(
                layout.database, workspace=workspace
            )
            try:
                if session_id is None:
                    if ui_mode == "fullscreen":
                        try:
                            session_id = await select_session_fullscreen(
                                await repositories.sessions.list(args.limit)
                            )
                        except Exception as exc:
                            errors.print(
                                "[error]Fullscreen TUI error:[/] "
                                f"{exc}\nUse `capslock resume --ui inline` "
                                "to run the terminal-native selector."
                            )
                            return 1
                    else:
                        session_id = await select_session(
                            output, repositories, args.limit
                        )
                    if session_id is None:
                        return 0
                else:
                    session = await repositories.sessions.resolve(session_id)
                    if session is None:
                        raise AgentRuntimeError(f"session does not exist: {session_id}")
                    session_id = session.id
            finally:
                await repositories.close()
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
                    limits=_exec_limits(application.agent.default_limits, args),
                )
            status_enabled = (
                not args.no_spinner
                and not args.quiet
                and dynamic_status_supported(
                    output.file, output_is_tty=output.is_terminal
                )
            )
            if ui_mode == "fullscreen":
                try:
                    return await run_fullscreen_tui(
                        context, status_enabled=status_enabled
                    )
                except Exception as exc:
                    errors.print(
                        "[error]Fullscreen TUI error:[/] "
                        f"{exc}\nUse `capslock --ui inline` to run the "
                        "terminal-native UI."
                    )
                    return 1
            return await run_tui(context, status_enabled=status_enabled)
    except KeyboardInterrupt:
        errors.print("\n[warning]Cancelled.[/]")
        return 130
    except (LayoutConflict, IncompatibleDatabaseError) as exc:
        errors.print(f"[error]Incompatible state:[/] {exc}")
        return 2
    except (AgentRuntimeError, CredentialError, LifecycleError, ValueError) as exc:
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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _ui_mode(args: argparse.Namespace) -> str:
    value = getattr(args, "ui", None) or os.environ.get("CAPSLOCK_UI") or "inline"
    normalized = value.strip().casefold()
    if normalized not in {"inline", "fullscreen"}:
        raise ValueError("CAPSLOCK_UI must be 'inline' or 'fullscreen'")
    return normalized


def _tighter(configured, requested):
    if requested is None:
        return configured
    if configured is None:
        return requested
    return min(configured, requested)


def _exec_limits(defaults: RunLimits, args) -> RunLimits:
    return RunLimits(
        max_tool_rounds=_tighter(defaults.max_tool_rounds, args.max_tool_rounds),
        max_tool_calls=args.max_tool_calls,
        max_duration_seconds=args.max_duration_seconds,
        max_tokens=_tighter(defaults.max_tokens, args.max_tokens),
        max_budget_usd=_tighter(defaults.max_budget_usd, args.max_budget_usd),
    )


if __name__ == "__main__":
    raise SystemExit(main())
