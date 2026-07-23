"""CapsLock command-line composition root."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from rich.console import Console

from .. import __version__
from ..environment import load_project_environment
from ..layout import ProjectLayout


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
    config_parser = subparsers.add_parser("config", help="Validate configuration")
    config_commands = config_parser.add_subparsers(dest="config_command")
    validate = config_commands.add_parser("validate")
    validate.add_argument("--json", action="store_true")
    validate.add_argument("--strict", action="store_true")
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
    portable_export.add_argument("--include-artifacts", action="store_true")
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
        if command == "enable":
            plugin_change.add_argument("--trusted-native", action="store_true")
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
    settings,
    session_id: str | None = None,
    *,
    layout: ProjectLayout | None = None,
):
    from ..bootstrap import WorkspaceApplication
    from .providers import create_provider_clients

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
    from ..theme import make_console

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
            from .setup import initialize

            return await initialize(output, workspace, args)
        if args.command == "config":
            from .setup import config_validate

            if args.config_command in {None, "validate"}:
                return await config_validate(
                    output,
                    layout.config,
                    json_output=getattr(args, "json", False),
                    strict=getattr(args, "strict", False),
                )
            raise ValueError("unknown config command")
        if args.command == "credentials":
            from .setup import credentials_command

            return await credentials_command(output, layout.config, args)
        if args.command in {"plugin", "plugins"}:
            from .plugins import plugin_command

            return await plugin_command(output, layout, args)
        if args.command == "doctor":
            from .diagnostics import doctor

            return await doctor(output, workspace, layout=layout, args=args)
        if args.command == "backup":
            from .lifecycle import backup_command

            return await backup_command(output, layout, args)
        if args.command == "export":
            from .lifecycle import export_lifecycle

            return await export_lifecycle(
                output,
                layout,
                args.destination,
                include_global_memory=args.include_global_memory,
                include_artifacts=args.include_artifacts,
            )
        if args.command == "import":
            from ..lifecycle import LifecycleError
            from .lifecycle import import_lifecycle

            journal = layout.root / "state" / "lifecycle-journal.json"
            if journal.exists():
                raise LifecycleError(
                    f"an incomplete lifecycle operation requires `capslock doctor --fix`: {journal}"
                )
            return await import_lifecycle(output, layout, args.archive, yes=args.yes)
        journal = layout.root / "state" / "lifecycle-journal.json"
        if journal.exists():
            from ..lifecycle.errors import LifecycleError

            raise LifecycleError(
                f"an incomplete lifecycle operation requires `capslock doctor --fix`: {journal}"
            )
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
        from ..configuration import Settings

        settings = Settings.load(workspace, layout=layout)
        if args.command in {"session", "sessions"}:
            return await _sessions(output, args, workspace, layout, settings)
        session_id = getattr(args, "session_id", None)
        ui_mode = _ui_mode(args) if args.command in {None, "resume"} else "inline"
        if args.command == "resume":
            from ..runtime import AgentRuntimeError
            from ..storage.repositories import WorkspaceRepositories
            from .diagnostics import select_session

            repositories = await WorkspaceRepositories.open(
                layout.database, workspace=workspace
            )
            try:
                if session_id is None:
                    if ui_mode == "fullscreen":
                        try:
                            from .fullscreen_tui import select_session_fullscreen

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
            from .context import CliContext

            context = CliContext(output, application.session, application.queries)
            if args.command == "exec":
                from .exec import run_exec

                return await run_exec(
                    context,
                    args.question,
                    json_events=args.json,
                    spinner=not args.no_spinner,
                    quiet=args.quiet,
                    limits=_exec_limits(application.session.default_limits, args),
                )
            from .status import dynamic_status_supported

            status_enabled = (
                not args.no_spinner
                and not args.quiet
                and dynamic_status_supported(
                    output.file, output_is_tty=output.is_terminal
                )
            )
            if ui_mode == "fullscreen":
                try:
                    from .fullscreen_tui import run_fullscreen_tui

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
            from .tui import run_tui

            return await run_tui(context, status_enabled=status_enabled)
    except KeyboardInterrupt:
        errors.print("\n[warning]Cancelled.[/]")
        return 130
    except Exception as exc:
        if type(exc).__name__ in {"LayoutConflict", "IncompatibleDatabaseError"}:
            errors.print(f"[error]Incompatible state:[/] {exc}")
            return 2
        if isinstance(exc, ValueError) or type(exc).__name__ in {
            "AgentRuntimeError",
            "CredentialError",
            "LifecycleError",
            "PluginValidationError",
            "SandboxUnavailableError",
        }:
            errors.print(f"[error]Error:[/] {exc}")
            return 2
        errors.print(f"[error]Model or transport error:[/] {exc}")
        return 1


async def _sessions(
    output: Console, args, workspace: Path, layout: ProjectLayout, settings
) -> int:
    from ..session_management import SessionManager
    from ..storage.memory_repositories import MemoryRepositories
    from ..storage.repositories import WorkspaceRepositories
    from .diagnostics import (
        archive_session,
        delete_session,
        export_session,
        list_sessions,
        rename_session,
        search_sessions,
    )
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


def _exec_limits(defaults, args):
    from ..domain import RunLimits

    return RunLimits(
        max_tool_rounds=_tighter(defaults.max_tool_rounds, args.max_tool_rounds),
        max_tool_calls=args.max_tool_calls,
        max_duration_seconds=args.max_duration_seconds,
        max_tokens=_tighter(defaults.max_tokens, args.max_tokens),
        max_budget_usd=_tighter(defaults.max_budget_usd, args.max_budget_usd),
    )


if __name__ == "__main__":
    raise SystemExit(main())
