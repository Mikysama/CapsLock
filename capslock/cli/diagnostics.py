"""Async session management and v2 diagnostics."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from rich.console import Console

from ..config import migrate_config, read_config_document, validate_config_document
from ..credentials import credential_status
from ..layout import ProjectLayout
from ..lifecycle import LifecycleService
from ..mcp import McpRegistry
from ..policy import WorkspacePolicy
from ..storage.schema_v2 import (
    MEMORY_APPLICATION_ID,
    MEMORY_SCHEMA_VERSION,
    WORKSPACE_APPLICATION_ID,
    WORKSPACE_SCHEMA_VERSION,
)
from ..skills import SkillRegistry
from ..session_management import SessionManager
from ..storage.repositories_v2 import WorkspaceRepositories
from .prompt import select_session as choose_session
from .views.diagnostics import render_doctor, render_sessions


async def list_sessions(
    console: Console, repositories: WorkspaceRepositories, limit: int
) -> int:
    render_sessions(console, await repositories.sessions.list(limit))
    return 0


async def rename_session(
    console: Console, repositories: WorkspaceRepositories, prefix: str, title: str
) -> int:
    session = await repositories.sessions.resolve(prefix)
    if session is None:
        raise ValueError(f"session does not exist: {prefix}")
    updated = await repositories.sessions.rename(session.id, title)
    console.print(f"[success]Renamed:[/] {updated.title}")
    return 0


async def search_sessions(
    console: Console,
    repositories: WorkspaceRepositories,
    query: str,
    limit: int,
    *,
    include_archived: bool = False,
) -> int:
    render_sessions(
        console,
        await repositories.sessions.search(
            query, limit=limit, include_archived=include_archived
        ),
    )
    return 0


async def archive_session(
    console: Console,
    repositories: WorkspaceRepositories,
    prefix: str,
    *,
    archived: bool,
) -> int:
    session = await repositories.sessions.resolve(prefix)
    if session is None:
        raise ValueError(f"session does not exist: {prefix}")
    await repositories.sessions.archive(session.id, archived=archived)
    console.print("[success]Session updated.[/]")
    return 0


async def export_session(
    console: Console,
    manager: SessionManager,
    repositories: WorkspaceRepositories,
    prefix: str,
    destination: str,
) -> int:
    session = await repositories.sessions.resolve(prefix)
    if session is None:
        raise ValueError(f"session does not exist: {prefix}")
    console.print(
        f"[success]Exported:[/] {await manager.export(session.id, destination)}"
    )
    return 0


async def delete_session(
    console: Console,
    manager: SessionManager,
    repositories: WorkspaceRepositories,
    prefix: str | None,
    *,
    yes: bool = False,
    limit: int = 20,
) -> int:
    interactive = prefix is None
    while True:
        selected = prefix
        if selected is None:
            selected = await select_session(
                console,
                repositories,
                limit,
                prompt_title="Delete a session",
            )
            if selected is None:
                return 0
        session = await repositories.sessions.resolve(selected)
        if session is None:
            raise ValueError(f"session does not exist: {selected}")
        if not yes:
            answer = await asyncio.to_thread(
                console.input,
                f'Permanently delete session "{session.title}" '
                f"({session.id[:12]})? [y/N] ",
            )
            if answer.strip().casefold() not in {"y", "yes"}:
                if interactive:
                    continue
                return 0
        purged = await manager.delete(session.id)
        console.print(f"[success]Deleted session; purged {purged} session memories.[/]")
        return 0


async def select_session(
    console: Console,
    repositories: WorkspaceRepositories,
    limit: int,
    *,
    prompt_title: str = "Resume a session",
) -> str | None:
    sessions = await repositories.sessions.list(limit)
    if not sessions:
        console.print("[text.secondary]No sessions available.[/]")
        return None
    return await asyncio.to_thread(
        choose_session,
        sessions,
        console.width,
        title=prompt_title,
    )


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    subject: str
    message: str
    fixable: bool = False


async def doctor(
    console: Console, workspace: Path, *, layout: ProjectLayout, args
) -> int:
    diagnostics = [
        Diagnostic("ok", "workspace", "Workspace", str(workspace)),
        Diagnostic("ok", "layout", "Layout", "v2 canonical"),
    ]
    config_valid = False
    if not layout.config.exists():
        diagnostics.append(
            Diagnostic(
                "error",
                "config_missing",
                "Project config",
                f"missing: {layout.config}; run `capslock init`",
            )
        )
    else:
        try:
            document = read_config_document(layout.config)
            issues = validate_config_document(document)
            config_valid = not any(item.severity == "error" for item in issues)
            diagnostics.extend(
                Diagnostic(
                    item.severity,
                    item.code,
                    item.path,
                    item.message,
                    item.code == "config_deprecated",
                )
                for item in issues
            )
            if not issues:
                diagnostics.append(
                    Diagnostic("ok", "config", "Project config", "version 1 valid")
                )
            for reference in _credential_references(document):
                status = credential_status(reference)
                diagnostics.append(
                    Diagnostic(
                        "ok" if status.available else "error",
                        "credential",
                        reference,
                        "available"
                        if status.available
                        else "missing or backend unavailable",
                    )
                )
        except ValueError as exc:
            diagnostics.append(
                Diagnostic("error", "config_parse", "Project config", str(exc))
            )
    for path, label, application_id, version in (
        (
            layout.database,
            "Workspace database",
            WORKSPACE_APPLICATION_ID,
            WORKSPACE_SCHEMA_VERSION,
        ),
        (
            layout.user.memory,
            "Memory database",
            MEMORY_APPLICATION_ID,
            MEMORY_SCHEMA_VERSION,
        ),
    ):
        diagnostics.extend(
            await asyncio.to_thread(
                _database_diagnostics, path, label, application_id, version
            )
        )
    journal = layout.root / "state" / "lifecycle-journal.json"
    if journal.exists():
        diagnostics.append(
            Diagnostic(
                "error",
                "lifecycle_incomplete",
                "Lifecycle operation",
                f"incomplete operation journal: {journal}",
                True,
            )
        )
    for path, label in (
        (layout.user.memory, "Memory database"),
        (layout.local_mcp, "Local MCP config"),
    ):
        if path.exists() and stat_mode(path) & 0o077:
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "private_permissions",
                    label,
                    f"permissions should be 0600: {path}",
                    True,
                )
            )
    if layout.project_mcp.exists() or layout.local_mcp.exists():
        try:
            McpRegistry(WorkspacePolicy(workspace), layout=layout).servers()
            diagnostics.append(Diagnostic("ok", "mcp", "MCP", "configuration valid"))
        except (ValueError, OSError) as exc:
            diagnostics.append(Diagnostic("error", "mcp", "MCP", str(exc)))
    skill_entries = SkillRegistry(workspace, layout=layout).entries()
    invalid_skills = [item for item in skill_entries if item.error]
    diagnostics.append(
        Diagnostic(
            "error" if invalid_skills else "ok",
            "skills",
            "Skills",
            (
                "; ".join(f"{item.name}: {item.error}" for item in invalid_skills)
                if invalid_skills
                else f"{len(skill_entries)} packages valid"
            ),
        )
    )
    if args.fix:
        await _apply_fixes(console, diagnostics, layout, yes=args.yes)
        clone = type("DoctorArgs", (), {**vars(args), "fix": False})()
        return await doctor(console, workspace, layout=layout, args=clone)
    if config_valid:
        try:
            from ..config import Settings

            settings = Settings.load(workspace, layout=layout)
            for name, provider in sorted((settings.providers or {}).items()):
                diagnostics.append(
                    Diagnostic(
                        "ok" if provider.api_key else "error",
                        "provider",
                        f"Provider {name}",
                        f"{provider.kind}; credential={'available' if provider.api_key else 'missing'}; policy={provider.data_policy}",
                    )
                )
            if args.network and settings.model_config.api_key:
                from .app import create_client

                client = create_client(settings)
                try:
                    await client.models.list()
                finally:
                    await client.close()
                diagnostics.append(
                    Diagnostic(
                        "ok", "provider_network", "Provider network", "connected"
                    )
                )
        except Exception as exc:
            diagnostics.append(Diagnostic("error", "settings", "Settings", str(exc)))
    if args.json:
        console.print_json(
            json.dumps({"diagnostics": [asdict(item) for item in diagnostics]})
        )
    else:
        render_doctor(
            console,
            [
                (f"{item.severity.upper()} {item.subject}", item.message)
                for item in diagnostics
            ],
        )
    has_error = any(item.severity == "error" for item in diagnostics)
    has_warning = any(item.severity == "warning" for item in diagnostics)
    return 1 if has_error or (args.strict and has_warning) else 0


def _database_diagnostics(
    path: Path, label: str, expected_id: int, expected_version: int
) -> list[Diagnostic]:
    if not path.exists():
        return [Diagnostic("warning", "database_new", label, f"new: {path}")]
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign = list(connection.execute("PRAGMA foreign_key_check"))
        connection.close()
    except sqlite3.Error as exc:
        return [Diagnostic("error", "database_open", label, str(exc))]
    output: list[Diagnostic] = []
    if app_id != expected_id:
        output.append(
            Diagnostic(
                "error",
                "database_application_id",
                label,
                f"application_id={app_id}, expected {expected_id}",
            )
        )
    elif version > expected_version or version <= 0:
        output.append(
            Diagnostic(
                "error",
                "database_schema",
                label,
                f"schema={version}, supported through {expected_version}",
            )
        )
    elif version < expected_version:
        output.append(
            Diagnostic(
                "warning",
                "database_migration",
                label,
                f"schema {version} will migrate to {expected_version}",
                True,
            )
        )
    else:
        output.append(
            Diagnostic(
                "ok",
                "database_schema",
                label,
                f"application_id={app_id} schema={version}",
            )
        )
    if integrity != "ok" or foreign:
        output.append(
            Diagnostic(
                "error",
                "database_integrity",
                label,
                f"quick_check={integrity}; foreign_key_errors={len(foreign)}",
            )
        )
    return output


def _credential_references(document: dict[str, object]) -> set[str]:
    references: set[str] = set()
    providers = document.get("providers")
    if isinstance(providers, dict):
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            if provider.get("credential"):
                references.add(str(provider["credential"]))
            elif provider.get("api_key_env"):
                references.add(f"env:{provider['api_key_env']}")
    web = document.get("web")
    if isinstance(web, dict) and web.get("tavily_credential"):
        references.add(str(web["tavily_credential"]))
    if not providers:
        references.add("env:CAPSLOCK_API_KEY")
    return references


async def _apply_fixes(
    console: Console,
    diagnostics: list[Diagnostic],
    layout: ProjectLayout,
    *,
    yes: bool,
) -> None:
    for item in [entry for entry in diagnostics if entry.fixable]:
        if not yes:
            answer = await asyncio.to_thread(
                console.input, f"Fix {item.subject}: {item.message}? [y/N] "
            )
            if answer.strip().casefold() not in {"y", "yes"}:
                continue
        if item.code == "config_deprecated":
            migrate_config(layout.config, apply=True)
        elif item.code == "private_permissions":
            path = (
                layout.user.memory
                if item.subject == "Memory database"
                else layout.local_mcp
            )
            path.chmod(0o600)
        elif item.code == "database_migration":
            if item.subject == "Workspace database":
                repositories = await WorkspaceRepositories.open(
                    layout.database, workspace=layout.workspace
                )
            else:
                from ..storage.memory_v2 import MemoryRepositories

                repositories = await MemoryRepositories.open(layout.user.memory)
            await repositories.close()
        elif item.code == "lifecycle_incomplete":
            journal = layout.root / "state" / "lifecycle-journal.json"
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
                recovery = Path(str(payload.get("recovery", "")))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("lifecycle journal cannot be recovered") from exc
            if not recovery.is_file():
                raise ValueError("lifecycle recovery backup is missing")
            await asyncio.to_thread(LifecycleService(layout).backup_restore, recovery)


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode
