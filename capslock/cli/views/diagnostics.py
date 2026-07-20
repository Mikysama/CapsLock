"""Session and doctor presentation."""

from __future__ import annotations

from rich.console import Console

from ...domain import SessionInfo
from .common import table


def render_sessions(console: Console, sessions: list[SessionInfo]) -> None:
    output = table("Title", "Updated", "Session")
    for item in sessions:
        output.add_row(item.title, item.updated_at, item.id[:12])
    console.print(output)


def render_doctor(console: Console, checks: list[tuple[str, str]]) -> None:
    output = table("Check", "Result")
    for name, result in checks:
        output.add_row(name, result)
    console.print(output)
