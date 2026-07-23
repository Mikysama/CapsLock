"""Workflow, event, status, queue, and history presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.text import Text

from ...domain import SessionInfo, TaskInfo, WorkItemInfo
from .common import status_text, table
from .conversation import assistant_content, user_message


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
    context_budget_tokens: int
    budget: dict[str, Any] | None = None


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
            (
                f"{view.context_messages} messages · {view.context_budget_tokens} token budget\n",
                "text.secondary",
            ),
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
    if view.budget:
        used = view.budget.get("used", {})
        limits = view.budget.get("limits", {})
        console.print(
            Text.assemble(
                ("run budget=", "text.muted"),
                (
                    f"{used.get('tool_rounds', 0)}/{limits.get('max_tool_rounds', '-')} rounds · "
                    f"{used.get('tool_calls', 0)}/{limits.get('max_tool_calls') or '-'} calls · "
                    f"{used.get('duration_ms', 0)}ms · {used.get('tokens', 0)} tokens · "
                    f"${float(used.get('budget_usd', 0)):.6f}",
                    "text.secondary",
                ),
            )
        )
        if view.budget.get("stop_reason"):
            console.print(f"[warning]stop reason:[/] {view.budget['stop_reason']}")
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
        content = str(entry.get("content", ""))
        if role == "user":
            console.print(user_message(content))
        else:
            console.print("[agent.bold]◆ CapsLock[/]")
            console.print(assistant_content(content))
        if entry.get("status") not in {None, "completed"}:
            console.print(
                f"[warning]{entry.get('status')}:[/] {entry.get('error') or ''}"
            )


def result_status(label: str, status: str, *, detail: str | None = None) -> Text:
    style = {
        "success": "success",
        "failed": "error",
        "cancelled": "warning",
        "stopped": "warning",
        "waiting": "waiting",
    }.get(status, "text.secondary")
    suffix = f" · {detail}" if detail else ""
    return Text.assemble(("● ", style), (label, style), (suffix, "text.muted"))
