"""Workspace diagnostics and saved-session commands."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from rich.console import Console

from ..config import Settings
from ..execution import CommandService
from ..layout import ProjectLayout
from ..policy import WorkspacePolicy
from ..session import SessionStore
from ..skills import SkillRegistry
from ..storage import MemoryStore, workspace_key
from .prompt import select_session
from .render import render_doctor, render_session_list, render_session_renamed


def open_store(workspace: Path, *, layout: ProjectLayout | None = None) -> SessionStore:
    selected = layout or ProjectLayout.discover(workspace)
    return SessionStore(selected.database)


def render_sessions(console: Console, store: SessionStore, limit: int) -> int:
    render_session_list(console, store.list(limit))
    return 0


def rename_saved_session(console: Console, store: SessionStore, prefix: str, title: str) -> int:
    try:
        session = store.resolve_session(prefix)
        if session is None:
            raise ValueError(f"session does not exist: {prefix}")
        renamed = store.rename_session(session.id, title)
    except ValueError as exc:
        console.print(f"[error]Error:[/] {exc}")
        return 2
    render_session_renamed(console, renamed)
    return 0


def select_saved_session(console: Console, store: SessionStore, limit: int) -> str | None:
    sessions = store.list(limit)
    if not sessions:
        console.print("[text.secondary]No saved sessions in this workspace.[/]")
        return None
    try:
        return select_session(sessions)
    except (EOFError, KeyboardInterrupt):
        console.print("\n[warning]Cancelled.[/]")
        return None


def doctor(console: Console, workspace: Path, settings: Settings, *, layout: ProjectLayout | None = None) -> int:
    selected = layout or ProjectLayout.discover(workspace)
    api_ready = bool(settings.api_key and not settings.api_key.startswith("your_"))
    git_ready = (workspace / ".git").exists()
    with open_store(workspace, layout=selected) as store:
        commands = CommandService(store, WorkspacePolicy(workspace), "doctor", "doctor", lambda *args, **kwargs: None)
        available = commands.available_templates()
        registry = SkillRegistry(
            workspace,
            disabled=lambda name: not store.skill_enabled(name),
            layout=selected,
        )
        catalog = registry.catalog()
        invalid_skills = sum(1 for entry in registry.entries() if entry.error is not None)
    memory_path = settings.memory.database or selected.user.memory
    with MemoryStore(memory_path) as memory:
        key = workspace_key(workspace)
        local_write = memory.local_write_enabled(key)
        memory_settings = memory.memory_settings(key)
        fts_available = memory.fts_available
    render_doctor(
        console,
        workspace=workspace,
        layout_mode=selected.mode,
        config=selected.config,
        project_mcp=selected.project_mcp,
        skills=selected.skills,
        skill_count=catalog.total,
        skill_catalog_bytes=catalog.bytes,
        skill_catalog_truncated=catalog.truncated,
        invalid_skills=invalid_skills,
        layout_warnings=selected.warnings,
        database=selected.database,
        model=settings.model,
        endpoint=safe_endpoint(settings.base_url),
        api_ready=api_ready,
        git_ready=git_ready,
        commands=available,
        memory_database=memory_path,
        memory_fts=fts_available,
        memory_write_enabled=settings.memory.project_write_enabled and local_write,
        memory_policy=memory_settings["policy"].value,
        memory_recall_enabled=bool(memory_settings["recall_enabled"]),
        memory_embedding_backend=memory_settings["embedding_backend"].value,
    )
    return 0


def safe_endpoint(endpoint: str) -> str:
    """Remove credentials and query data before diagnostic display."""
    parsed = urlsplit(endpoint)
    if not parsed.hostname:
        return "<invalid endpoint>"
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "<invalid endpoint>"
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))
