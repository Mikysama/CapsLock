"""Workflow, event, status, queue, and history presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.text import Text

from ...domain import SessionInfo, TaskInfo, WorkItemInfo
from .common import status_text, table


@dataclass(frozen=True)
class StatusView:
    session: SessionInfo
    workspace: str
    model: str
    permission_mode: str
    tasks: list[TaskInfo]
    work_items: list[WorkItemInfo]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    context_messages: int
    context_limit: int


def render_status(console: Console, view: StatusView) -> None:
    console.print(
        Text.assemble(
            ("title=", "text.muted"),
            (f"{view.session.title}\n", "text.primary"),
            ("session=", "text.muted"),
            (f"{view.session.id}\n", "text.secondary"),
            ("workspace=", "text.muted"),
            (f"{view.workspace}\n", "path"),
            ("model=", "text.muted"),
            (f"{view.model}\n", "text.secondary"),
            ("permission=", "text.muted"),
            (f"{view.permission_mode}\n", "waiting"),
            ("context=", "text.muted"),
            (f"{view.context_messages}/{view.context_limit}\n", "text.secondary"),
            ("usage=", "text.muted"),
            (
                f"{view.input_tokens} input / {view.output_tokens} output / ${view.cost_usd:.6f}",
                "text.secondary",
            ),
        )
    )
    if view.tasks:
        output = table("Task", "Status", "Text")
        for item in view.tasks:
            output.add_row(item.id[:12], status_text(item.status), item.text)
        console.print(output)
    render_queue(console, view.work_items)


def render_queue(console: Console, items: list[WorkItemInfo]) -> None:
    if not items:
        console.print("[text.secondary]Queue empty.[/]")
        return
    output = table("Position", "Work item", "Status", "Request")
    for item in items:
        output.add_row(
            str(item.position),
            item.id[:12],
            status_text(item.status.value),
            item.question,
        )
    console.print(output)


def render_history(
    console: Console, session: SessionInfo, transcript: list[dict[str, Any]]
) -> None:
    if not transcript:
        return
    console.print(
        f"[text.secondary]Resumed:[/] {session.title} [text.muted]({session.id[:12]})[/]"
    )
    for entry in transcript:
        role = str(entry["role"])
        console.print(
            "\n[user.bold]You[/]" if role == "user" else "\n[agent.bold]CapsLock[/]"
        )
        console.print(str(entry.get("content", "")), markup=False, highlight=False)
        if entry.get("status") not in {None, "completed"}:
            console.print(
                f"[warning]{entry.get('status')}:[/] {entry.get('error') or ''}"
            )


def result_status(label: str, status: str, *, detail: str | None = None) -> Text:
    style = {
        "success": "success",
        "failed": "error",
        "cancelled": "warning",
        "waiting": "waiting",
    }.get(status, "text.secondary")
    suffix = f" · {detail}" if detail else ""
    return Text.assemble(("● ", style), (label, style), (suffix, "text.muted"))
