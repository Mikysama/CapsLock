"""Workflow domain types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class WorkItemStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"


class RunStepKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    APPROVAL = "approval"


class RunStepStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentEventKind(StrEnum):
    QUEUED = "queued"
    THINKING = "thinking"
    TEXT_DELTA = "text_delta"
    TOOL_RUNNING = "tool_running"
    TOOL_COMPLETED = "tool_completed"
    BUDGET_UPDATED = "budget_updated"
    LIMIT_REACHED = "limit_reached"
    BUDGET_EXTENDED = "budget_extended"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


TERMINAL_EVENT_KINDS = frozenset(
    {
        AgentEventKind.WAITING_APPROVAL,
        AgentEventKind.COMPLETED,
        AgentEventKind.FAILED,
        AgentEventKind.CANCELLED,
        AgentEventKind.STOPPED,
    }
)


@dataclass(frozen=True)
class WorkItemInfo:
    id: str
    session_id: str
    question: str
    status: WorkItemStatus
    position: int
    created_at: str
    updated_at: str
    current_run_id: str | None = None
    parent_work_item_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RunStepInfo:
    id: str
    run_id: str
    ordinal: int
    kind: RunStepKind
    status: RunStepStatus
    checkpoint: dict[str, Any] | None
    started_at: str
    finished_at: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class AgentEvent:
    sequence: int
    timestamp: str
    session_id: str
    run_id: str
    work_item_id: str
    kind: AgentEventKind
    data: dict[str, Any]

    @property
    def terminal(self) -> bool:
        return self.kind in TERMINAL_EVENT_KINDS


@dataclass(frozen=True)
class RunInfo:
    id: str
    session_id: str
    work_item_id: str
    question: str
    status: str
    started_at: str
    finished_at: str | None = None
    duration_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0
    error_code: str | None = None
    error_message: str | None = None
    parent_run_id: str | None = None
    resume_from_step_id: str | None = None
    stop_reason: str | None = None
