"""Renderer-neutral agent activity states derived from workflow events."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from .domain import AgentEventKind


class AgentStatus(StrEnum):
    IDLE = "idle"
    THINKING = "thinking"
    READING = "reading"
    TOOL_CALLING = "tool_calling"
    ANALYZING = "analyzing"
    GENERATING = "generating"
    WAITING = "waiting"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


STATUS_MESSAGES: dict[AgentStatus, str] = {
    AgentStatus.IDLE: "Idle",
    AgentStatus.THINKING: "Thinking...",
    AgentStatus.READING: "Reading files...",
    AgentStatus.TOOL_CALLING: "Running tool...",
    AgentStatus.ANALYZING: "Analyzing results...",
    AgentStatus.GENERATING: "Generating response...",
    AgentStatus.WAITING: "Waiting for input...",
    AgentStatus.DONE: "Done",
    AgentStatus.ERROR: "Failed",
    AgentStatus.CANCELLED: "Cancelled",
    AgentStatus.STOPPED: "Stopped",
}

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

_READING_TOOLS = frozenset(
    {
        "get_memory",
        "git_diff",
        "git_status",
        "list_external_sources",
        "list_files",
        "load_skill",
        "read_file",
        "read_skill_resource",
        "search_files",
        "search_memories",
    }
)


def status_message(status: AgentStatus, detail: str | None = None) -> str:
    """Return the single centralized user-facing label for a status."""
    message = STATUS_MESSAGES[status]
    if not detail or status not in {AgentStatus.READING, AgentStatus.TOOL_CALLING}:
        return message
    stem = message[:-3] if message.endswith("...") else message
    return f"{stem}: {detail}..."


def status_for_event(
    kind: AgentEventKind, data: dict[str, Any] | None = None
) -> tuple[AgentStatus, str | None]:
    """Translate core workflow events without coupling the core to a terminal UI."""
    payload = data or {}
    if kind in {AgentEventKind.QUEUED, AgentEventKind.THINKING}:
        return AgentStatus.THINKING, None
    if kind is AgentEventKind.TOOL_RUNNING:
        name = str(payload.get("name", "unknown"))
        status = (
            AgentStatus.READING if name in _READING_TOOLS else AgentStatus.TOOL_CALLING
        )
        return status, name
    if kind is AgentEventKind.TOOL_COMPLETED:
        return AgentStatus.ANALYZING, None
    if kind is AgentEventKind.TEXT_DELTA:
        return AgentStatus.GENERATING, None
    if kind in {
        AgentEventKind.WAITING_APPROVAL,
        AgentEventKind.WAITING_INPUT,
    }:
        return AgentStatus.WAITING, None
    if kind is AgentEventKind.LIMIT_REACHED:
        return AgentStatus.WAITING, None
    if kind in {AgentEventKind.BUDGET_UPDATED, AgentEventKind.BUDGET_EXTENDED}:
        return AgentStatus.ANALYZING, None
    if kind is AgentEventKind.COMPLETED:
        return AgentStatus.DONE, None
    if kind is AgentEventKind.CANCELLED:
        return AgentStatus.CANCELLED, None
    if kind is AgentEventKind.FAILED:
        return AgentStatus.ERROR, None
    if kind is AgentEventKind.STOPPED:
        return AgentStatus.STOPPED, None
    return AgentStatus.IDLE, None
