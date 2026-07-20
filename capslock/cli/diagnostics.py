"""Async session management and v2 diagnostics."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from rich.console import Console

from ..config import Settings
from ..layout import ProjectLayout
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


async def doctor(
    console: Console, workspace: Path, settings: Settings, *, layout: ProjectLayout
) -> int:
    checks = [
        ("Workspace", str(workspace)),
        ("Layout", "v2 canonical"),
        ("Project config", str(layout.config)),
        (
            "Workspace database",
            await asyncio.to_thread(_database_status, layout.database),
        ),
        (
            "Memory database",
            await asyncio.to_thread(
                _database_status,
                settings.memory.database or layout.user.canonical_memory,
            ),
        ),
        ("Model", settings.model_config.model),
        ("API key", "configured" if settings.model_config.api_key else "missing"),
        ("Git", "repository" if (workspace / ".git").is_dir() else "not a repository"),
    ]
    render_doctor(console, checks)
    return 0


def _database_status(path: Path) -> str:
    if not path.exists():
        return f"new ({path})"
    connection = sqlite3.connect(path)
    try:
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    return f"application_id={app_id} schema={version} ({path})"
