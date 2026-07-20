"""Action approval and source presentation."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ...domain import ActionRecord, SourceInfo
from ...security import redact
from .common import status_text, table


def render_approvals(console: Console, actions: list[ActionRecord]) -> None:
    if not actions:
        console.print("[text.secondary]No pending approvals.[/]")
        return
    output = table("Action", "Risk", "Type", "Status", "Summary")
    for item in actions:
        output.add_row(
            item.id[:12],
            item.risk_level or "unknown",
            item.type.value,
            status_text(item.status.value),
            item.summary,
        )
    console.print(output)
    for item in actions:
        console.print(
            Panel(
                Text(str(redact(item.request)), style="tool"),
                title=f"{item.id[:12]} · {item.type.value}",
                border_style="warning",
            )
        )


def render_sources(console: Console, sources: list[SourceInfo]) -> None:
    if not sources:
        console.print("[text.secondary]No external sources.[/]")
        return
    output = table("Source", "Title", "URL", "Flags")
    for item in sources:
        output.add_row(
            item.id[:12],
            item.title[:60],
            item.url,
            "untrusted/injection" if item.suspicious else "untrusted",
        )
    console.print(output)
