"""Temporary composition protocol for legacy runtime constructors."""

from __future__ import annotations

from typing import Any, Protocol

from .action import ActionRepositoryPort
from .model import GovernancePort, ModelAuditPort
from .workflow import RunJournal, SessionPort, SourcePort, TaskPort


class WorkspaceServicesPort(Protocol):
    sessions: SessionPort
    workflow: RunJournal
    actions: ActionRepositoryPort
    tasks: TaskPort
    sources: SourcePort
    models: ModelAuditPort
    governance: GovernancePort
    collaboration: Any
