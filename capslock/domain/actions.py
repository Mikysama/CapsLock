"""Action domain types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ActionType(StrEnum):
    FILE_EDIT = "file_edit"
    FILE_CREATE = "file_create"
    COMMAND = "command"
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    MCP_CONNECT = "mcp_connect"
    MCP_CALL = "mcp_call"


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ActionResultKind(StrEnum):
    APPLIED = "applied"
    UNDONE = "undone"
    EXIT_ZERO = "exit_zero"
    NONZERO_EXIT = "nonzero_exit"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    USER_CANCELLED = "user_cancelled"
    SUCCESS = "success"


@dataclass(frozen=True)
class ActionInfo:
    id: str
    session_id: str
    run_id: str
    type: ActionType
    status: ActionStatus
    result_kind: ActionResultKind | None
    summary: str
    created_at: str
    approved_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    reversed_at: str | None = None
    error: str | None = None
    risk_level: str | None = None
    risk_reason: str | None = None
    rollback: str | None = None
    decided_at: str | None = None


@dataclass(frozen=True)
class ChangeInfo:
    id: str
    session_id: str
    run_id: str
    path: str
    operation: str
    expected_hash: str | None
    before_content: str | None
    after_content: str
    diff: str
    summary: str
    status: ActionStatus
    created_at: str
    result_kind: ActionResultKind | None = None
    error: str | None = None


@dataclass(frozen=True)
class CommandInfo:
    id: str
    session_id: str
    run_id: str
    template: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: float
    summary: str
    status: ActionStatus
    exit_code: int | None
    stdout: str
    stderr: str
    result_kind: ActionResultKind | None = None
    error: str | None = None


@dataclass(frozen=True)
class ExternalActionInfo:
    id: str
    session_id: str
    run_id: str
    kind: str
    payload: dict[str, Any]
    summary: str
    status: ActionStatus
    result: dict[str, Any] | None
    error: str | None
    result_kind: ActionResultKind | None = None


@dataclass(frozen=True)
class ActionRecord:
    id: str
    session_id: str
    run_id: str
    type: ActionType
    status: ActionStatus
    summary: str
    request: dict[str, Any]
    result: dict[str, Any] | None
    result_kind: ActionResultKind | None
    created_at: str
    approved_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    reversed_at: str | None = None
    decided_at: str | None = None
    risk_level: str | None = None
    risk_reason: str | None = None
    rollback: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    import_id: str | None = None
    historical_only: bool = False
    requires_reapproval: bool = False
