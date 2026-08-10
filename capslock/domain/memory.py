"""Memory domain types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MemoryScope(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    SESSION = "session"
    AGENT = "agent"


class MemoryType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    TODO = "todo"
    NOTE = "note"
    PROJECT = "project"
    TEMPORARY = "temporary"


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


class MemoryDurability(StrEnum):
    TEMPORARY = "temporary"
    SESSION = "session"
    PROJECT = "project"
    DURABLE = "durable"


class MemoryRelationType(StrEnum):
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    SUPERSEDES = "supersedes"


class MemoryJobType(StrEnum):
    EXTRACT_RUN = "extract_run"
    CONSOLIDATE_WORKSPACE = "consolidate_workspace"
    PROMOTE_AGENT_MEMORY = "promote_agent_memory"


class MemoryJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


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
    namespace: str | None = None
    subject: str | None = None
    durability: MemoryDurability = MemoryDurability.DURABLE
    why: str | None = None
    how_to_apply: str | None = None
    last_verified_at: str | None = None
    owner_session_id: str | None = None
    project_instance_id: str | None = None


@dataclass(frozen=True)
class MemoryCandidateSourceInfo:
    message_id: str | None
    evidence_id: str | None
    quote: str
    direct: bool
    verified: bool


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
    source_message_id: str | None = None
    source_evidence_id: str | None = None
    source_quote: str | None = None
    direct: bool = False
    verified: bool = False
    namespace: str | None = None
    subject: str | None = None
    durability: MemoryDurability = MemoryDurability.DURABLE
    why: str | None = None
    how_to_apply: str | None = None
    extractor_confidence: float = 0.0
    verifier_confidence: float | None = None
    verification_status: str = "unverified"
    instruction_like: bool = False
    calibration_version: str | None = None
    sources: tuple[MemoryCandidateSourceInfo, ...] = ()


@dataclass(frozen=True)
class MemoryRecallHit:
    memory: MemoryInfo
    score: float
    lexical_rank: int | None
    semantic_rank: int | None
    reasons: tuple[str, ...]
    cosine: float | None = None
    retrieval_score: float = 0.0
    selected_reason: str | None = None
    filter_reason: str | None = None
