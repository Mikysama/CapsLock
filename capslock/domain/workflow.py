"""Workflow domain types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class WorkItemStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"


WORK_ITEM_TRANSITIONS: dict[WorkItemStatus, frozenset[WorkItemStatus]] = {
    WorkItemStatus.QUEUED: frozenset(
        {WorkItemStatus.RUNNING, WorkItemStatus.CANCELLED}
    ),
    WorkItemStatus.RUNNING: frozenset(
        {
            WorkItemStatus.WAITING_APPROVAL,
            WorkItemStatus.WAITING_INPUT,
            WorkItemStatus.COMPLETED,
            WorkItemStatus.FAILED,
            WorkItemStatus.CANCELLED,
            WorkItemStatus.INTERRUPTED,
            WorkItemStatus.STOPPED,
        }
    ),
    WorkItemStatus.WAITING_APPROVAL: frozenset(
        {
            WorkItemStatus.COMPLETED,
            WorkItemStatus.FAILED,
            WorkItemStatus.CANCELLED,
        }
    ),
    WorkItemStatus.WAITING_INPUT: frozenset(
        {
            WorkItemStatus.RUNNING,
            WorkItemStatus.FAILED,
            WorkItemStatus.CANCELLED,
        }
    ),
}

FINALIZABLE_WORK_ITEM_STATUSES = frozenset(
    {
        WorkItemStatus.WAITING_APPROVAL,
        WorkItemStatus.WAITING_INPUT,
        WorkItemStatus.COMPLETED,
        WorkItemStatus.FAILED,
        WorkItemStatus.CANCELLED,
        WorkItemStatus.INTERRUPTED,
        WorkItemStatus.STOPPED,
    }
)


def validate_work_item_transition(
    current: WorkItemStatus, target: WorkItemStatus
) -> None:
    if target is current:
        return
    if target not in WORK_ITEM_TRANSITIONS.get(current, frozenset()):
        raise ValueError(
            f"invalid work item transition: {current.value} -> {target.value}"
        )


def validate_final_status(status: WorkItemStatus) -> None:
    if status not in FINALIZABLE_WORK_ITEM_STATUSES:
        raise ValueError(f"unsupported workflow final status: {status.value}")


def interrupted_step_status(status: WorkItemStatus) -> "RunStepStatus | None":
    if status is WorkItemStatus.CANCELLED:
        return RunStepStatus.CANCELLED
    if status in {
        WorkItemStatus.FAILED,
        WorkItemStatus.INTERRUPTED,
        WorkItemStatus.STOPPED,
    }:
        return RunStepStatus.FAILED
    return None


@dataclass(frozen=True)
class ApprovalOutcome:
    status: WorkItemStatus
    event_kind: "AgentEventKind"
    error_code: str | None = None
    error_message: str | None = None


def approval_outcome(
    failed_status: str | None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ApprovalOutcome:
    if failed_status is None:
        return ApprovalOutcome(WorkItemStatus.COMPLETED, AgentEventKind.COMPLETED)
    cancelled = failed_status == "cancelled"
    status = WorkItemStatus.CANCELLED if cancelled else WorkItemStatus.FAILED
    kind = AgentEventKind.CANCELLED if cancelled else AgentEventKind.FAILED
    return ApprovalOutcome(
        status,
        kind,
        error_code or status.value,
        error_message or f"action {status.value}",
    )


class RunStepKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    APPROVAL = "approval"


class RunStepStatus(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentEventKind(StrEnum):
    QUEUED = "queued"
    THINKING = "thinking"
    TEXT_DELTA = "text_delta"
    TOOL_QUEUED = "tool_queued"
    TOOL_RUNNING = "tool_running"
    TOOL_PROGRESS = "tool_progress"
    TOOL_PERMISSION = "tool_permission"
    TOOL_COMPLETED = "tool_completed"
    TOOL_CANCELLED = "tool_cancelled"
    BUDGET_UPDATED = "budget_updated"
    LIMIT_REACHED = "limit_reached"
    BUDGET_EXTENDED = "budget_extended"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


TERMINAL_EVENT_KINDS = frozenset(
    {
        AgentEventKind.WAITING_APPROVAL,
        AgentEventKind.WAITING_INPUT,
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
    event_id: str = ""
    trace_id: str = ""

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
