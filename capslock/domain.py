"""Domain types shared by the application, storage, and presentation layers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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


class ActionResultKind(StrEnum):
    APPLIED = "applied"
    UNDONE = "undone"
    EXIT_ZERO = "exit_zero"
    NONZERO_EXIT = "nonzero_exit"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    USER_CANCELLED = "user_cancelled"
    SUCCESS = "success"


class MemoryScope(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    SESSION = "session"


class MemoryType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    TODO = "todo"
    NOTE = "note"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    FORGOTTEN = "forgotten"
    PURGED = "purged"


class MemoryPolicy(StrEnum):
    OFF = "off"
    REVIEW = "review"
    AUTOMATIC = "automatic"


class MemoryCandidateStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    PURGED = "purged"


class MemoryOrigin(StrEnum):
    MANUAL = "manual"
    IMPORTED = "imported"
    REVIEWED = "reviewed"
    AUTOMATIC = "automatic"


class EmbeddingBackend(StrEnum):
    OFF = "off"
    FASTEMBED = "fastembed"
    LOCAL_HTTP = "local_http"


class SessionTitleSource(StrEnum):
    PENDING = "pending"
    FIRST_QUESTION = "first_question"
    MANUAL = "manual"


class WorkItemStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


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
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


MAX_SESSION_TITLE_LENGTH = 80


def normalize_session_title(value: str, *, truncate: bool = False) -> str:
    title = " ".join(value.split())
    if not title:
        raise ValueError("session title cannot be empty")
    if len(title) <= MAX_SESSION_TITLE_LENGTH:
        return title
    if truncate:
        return title[: MAX_SESSION_TITLE_LENGTH - 3].rstrip() + "..."
    raise ValueError(f"session title cannot exceed {MAX_SESSION_TITLE_LENGTH} characters")


def pending_session_title(created_at: str) -> str:
    timestamp = created_at[:16].replace("T", " ")
    return f"New session - {timestamp}"


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
class SessionInfo:
    id: str
    workspace: Path
    model: str
    created_at: str
    updated_at: str
    title: str = ""
    title_source: SessionTitleSource = SessionTitleSource.PENDING
    title_updated_at: str | None = None
    archived_at: str | None = None
    deletion_state: str | None = None


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
class TaskInfo:
    id: str
    session_id: str
    text: str
    status: str
    run_id: str | None = None
    position: int = 0


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
class SourceInfo:
    id: str
    session_id: str
    run_id: str
    url: str
    title: str
    excerpt: str
    fetched_at: str
    suspicious: bool


@dataclass(frozen=True)
class MemoryInfo:
    id: str
    content: str | None
    type: MemoryType
    scope: MemoryScope
    workspace_key: str | None
    session_id: str | None
    source_kind: str
    source_ref: str | None
    confidence: float
    expires_at: str | None
    revision: int
    status: MemoryStatus
    created_at: str
    updated_at: str
    purged_at: str | None = None
    origin: MemoryOrigin = MemoryOrigin.MANUAL
    source_valid: bool = True


@dataclass(frozen=True)
class MemoryCandidateInfo:
    id: str
    extraction_id: str
    content: str | None
    type: MemoryType
    scope: MemoryScope
    workspace_key: str
    session_id: str
    source_run_id: str
    confidence: float
    status: MemoryCandidateStatus
    relation: str
    related_memory_id: str | None
    risk_flags: tuple[str, ...]
    adopted_memory_id: str | None
    created_at: str
    decided_at: str | None = None


@dataclass(frozen=True)
class MemoryRecallHit:
    memory: MemoryInfo
    score: float
    lexical_rank: int | None
    semantic_rank: int | None
    reasons: tuple[str, ...]
