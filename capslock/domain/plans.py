"""Session-scoped planning state and immutable revision records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PlanStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    IMPLEMENTATION_FAILED = "implementation_failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class PlanRequestKind(StrEnum):
    ENTER = "enter"
    SUBMIT = "submit"


class PlanRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    FEEDBACK = "feedback"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class PlanApprovalChoice(StrEnum):
    ENTER = "enter"
    IMPLEMENT = "implement"
    FEEDBACK = "feedback"
    REJECT = "reject"


@dataclass(frozen=True)
class PlanRecord:
    id: str
    session_id: str
    objective: str
    status: PlanStatus
    entry_source: str
    base_permission_mode: str
    current_revision_id: str | None
    parent_plan_id: str | None
    mirror_relative_path: str
    created_at: str
    updated_at: str

    @property
    def active(self) -> bool:
        return self.status in {
            PlanStatus.DRAFT,
            PlanStatus.AWAITING_APPROVAL,
        }


@dataclass(frozen=True)
class PlanRevision:
    id: str
    plan_id: str
    ordinal: int
    content: str
    sha256: str
    source: str
    created_by_run_id: str | None
    created_at: str


@dataclass(frozen=True)
class PlanRequest:
    id: str
    session_id: str
    plan_id: str | None
    revision_id: str | None
    kind: PlanRequestKind
    status: PlanRequestStatus
    run_id: str | None
    invocation_id: str | None
    objective: str | None
    choice: str | None
    feedback: str | None
    created_at: str
    decided_at: str | None


@dataclass(frozen=True)
class PlanImplementation:
    plan_id: str
    revision_id: str
    request_id: str
    work_item_id: str
    run_id: str | None
    status: str
    created_at: str
    updated_at: str


__all__ = [
    "PlanApprovalChoice",
    "PlanImplementation",
    "PlanRecord",
    "PlanRequest",
    "PlanRequestKind",
    "PlanRequestStatus",
    "PlanRevision",
    "PlanStatus",
]
