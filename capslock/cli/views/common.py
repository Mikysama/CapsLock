"""Shared Rich presentation primitives."""

from __future__ import annotations

from rich.align import Align
from rich.console import Console
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ... import __version__
from ...permissions import PermissionMode


_CAPSLOCK_FONT = {
    "C": ("⇪⇪⇪", "⇪  ", "⇪  ", "⇪  ", "⇪⇪⇪"),
    "A": ("⇪⇪⇪", "⇪ ⇪", "⇪⇪⇪", "⇪ ⇪", "⇪ ⇪"),
    "P": ("⇪⇪⇪", "⇪ ⇪", "⇪⇪⇪", "⇪  ", "⇪  "),
    "S": ("⇪⇪⇪", "⇪  ", "⇪⇪⇪", "  ⇪", "⇪⇪⇪"),
    "L": ("⇪  ", "⇪  ", "⇪  ", "⇪  ", "⇪⇪⇪"),
    "O": ("⇪⇪⇪", "⇪ ⇪", "⇪ ⇪", "⇪ ⇪", "⇪⇪⇪"),
    "K": ("⇪ ⇪", "⇪⇪ ", "⇪  ", "⇪⇪ ", "⇪ ⇪"),
}
CAPSLOCK_ART = tuple(
    "  ".join(_CAPSLOCK_FONT[letter][row] for letter in "CAPSLOCK") for row in range(5)
)


def capslock_logo() -> Text:
    logo = Text(no_wrap=True)
    for index, line in enumerate(CAPSLOCK_ART):
        logo.append(line, style="primary.bold")
        if index < len(CAPSLOCK_ART) - 1:
            logo.append("\n")
    return logo


def table(*columns: str) -> Table:
    output = Table(
        show_header=True, header_style="primary.bold", border_style="border.muted"
    )
    for column in columns:
        output.add_column(column)
    return output


def status_text(status: str) -> Text:
    style = {
        "completed": "success",
        "active": "success",
        "running": "running",
        "waiting_approval": "waiting",
        "pending": "waiting",
        "failed": "error",
        "cancelled": "warning",
        "rejected": "warning",
    }.get(status, "text.secondary")
    return Text(status, style=style)


def startup(
    console: Console,
    *,
    workspace: str,
    model: str,
    session_id: str,
    permission_mode: PermissionMode,
) -> None:
    identity = Text.assemble(
        ("Welcome back!\n", "text.primary.bold"),
        (model, "text.secondary"),
        ("  ·  ", "text.muted"),
        (permission_mode.value, "waiting"),
        (f"\n{session_id[:12]}", "text.muted"),
        ("\n", "text.muted"),
        (workspace, "path"),
    )
    tips = Text.assemble(
        ("Tips for getting started\n", "primary.bold"),
        ("Type ", "text.primary"),
        ("/", "command.bold"),
        (" to browse commands and ", "text.primary"),
        ("/status", "command.bold"),
        (" to inspect this session.\n\n", "text.primary"),
        (
            "CapsLock keeps workspace actions visible, reviewable, and reversible.",
            "text.secondary",
        ),
    )
    if console.width < 110:
        body = Group(Align.center(identity), Text(""), Align.center(capslock_logo()))
    else:
        body = Table.grid(expand=True, padding=(0, 3))
        body.add_column(ratio=2)
        body.add_column(ratio=3)
        body.add_row(
            Align.center(Group(identity, Text(""), capslock_logo()), vertical="middle"),
            tips,
        )
    console.print(
        Panel(
            body,
            title=Text(f" CapsLock v{__version__} ", style="agent.bold"),
            title_align="left",
            border_style="border.focus",
            padding=(1, 2),
        )
    )


def error(console: Console, exc: Exception) -> None:
    console.print(f"[error]Error:[/] {exc}")
