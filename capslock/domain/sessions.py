"""Session domain types and title rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SessionTitleSource(StrEnum):
    PENDING = "pending"
    FIRST_QUESTION = "first_question"
    MANUAL = "manual"


MAX_SESSION_TITLE_LENGTH = 80


def normalize_session_title(value: str, *, truncate: bool = False) -> str:
    title = " ".join(value.split())
    if not title:
        raise ValueError("session title cannot be empty")
    if len(title) <= MAX_SESSION_TITLE_LENGTH:
        return title
    if truncate:
        return title[: MAX_SESSION_TITLE_LENGTH - 3].rstrip() + "..."
    raise ValueError(
        f"session title cannot exceed {MAX_SESSION_TITLE_LENGTH} characters"
    )


def pending_session_title(created_at: str) -> str:
    timestamp = created_at[:16].replace("T", " ")
    return f"New session - {timestamp}"


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
class TaskInfo:
    id: str
    session_id: str
    subject: str
    status: str
    run_id: str | None = None
    position: int = 0
    description: str = ""
    owner: str | None = None
    active_form: str | None = None
    metadata: dict[str, object] | None = None
    blocked_by: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """Storage compatibility for non-tool callers migrating to subject."""
        return self.subject
