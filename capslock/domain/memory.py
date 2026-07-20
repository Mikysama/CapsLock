"""Memory domain types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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
    EXTERNAL = "external"


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
