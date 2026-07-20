"""Memory presentation."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ...domain import MemoryCandidateInfo, MemoryInfo
from .common import status_text, table


def render_memories(
    console: Console, items: list[MemoryInfo], *, title: str = "Memories"
) -> None:
    if not items:
        console.print("[text.secondary]No matching memories.[/]")
        return
    output = table("Memory", "Status", "Type", "Scope", "Confidence", "Content")
    output.title = title
    for item in items:
        output.add_row(
            item.id[:12],
            status_text(item.status.value),
            item.type.value,
            item.scope.value,
            f"{item.confidence:g}",
            (item.content or "(purged)")[:80],
        )
    console.print(output)


def render_memory(console: Console, item: MemoryInfo) -> None:
    console.print(
        Panel(
            Text.assemble(
                ("id=", "text.muted"),
                (f"{item.id}\n", "text.secondary"),
                ("type=", "text.muted"),
                (f"{item.type.value}\n", "text.secondary"),
                ("scope=", "text.muted"),
                (f"{item.scope.value}\n", "text.secondary"),
                ("status=", "text.muted"),
                (f"{item.status.value}\n", "text.secondary"),
                ("revision=", "text.muted"),
                (f"{item.revision}\n\n", "text.secondary"),
                (item.content or "(purged)", "text.primary"),
            ),
            title=f"Memory {item.id[:12]}",
        )
    )


def render_candidates(console: Console, items: list[MemoryCandidateInfo]) -> None:
    if not items:
        console.print("[text.secondary]No memory candidates.[/]")
        return
    output = table(
        "Candidate", "Status", "Type", "Scope", "Relation", "Risks", "Content"
    )
    for item in items:
        output.add_row(
            item.id[:12],
            status_text(item.status.value),
            item.type.value,
            item.scope.value,
            item.relation,
            ", ".join(item.risk_flags) or "-",
            (item.content or "(cleared)")[:80],
        )
    console.print(output)
