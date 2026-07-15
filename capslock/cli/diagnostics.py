"""Workspace diagnostics and saved-session commands."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from rich.console import Console

from ..config import Settings
from ..execution import CommandService
from ..policy import WorkspacePolicy
from ..session import SessionStore
from .render import render_doctor, render_session_list


def open_store(workspace: Path) -> SessionStore:
    return SessionStore(workspace / ".capslock" / "capslock.sqlite3")


def render_sessions(console: Console, store: SessionStore, limit: int) -> int:
    render_session_list(console, store.list(limit))
    return 0


def doctor(console: Console, workspace: Path, settings: Settings) -> int:
    api_ready = bool(settings.api_key and not settings.api_key.startswith("your_"))
    git_ready = (workspace / ".git").exists()
    with open_store(workspace) as store:
        commands = CommandService(store, WorkspacePolicy(workspace), "doctor", "doctor", lambda *args, **kwargs: None)
        available = commands.available_templates()
    render_doctor(
        console,
        workspace=workspace,
        database=workspace / ".capslock" / "capslock.sqlite3",
        model=settings.model,
        endpoint=safe_endpoint(settings.base_url),
        api_ready=api_ready,
        git_ready=git_ready,
        commands=available,
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
