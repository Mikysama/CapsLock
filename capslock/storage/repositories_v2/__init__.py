"""Composed async workspace repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..async_database import WorkspaceDatabase
from .actions import ActionRepository
from .misc import (
    SettingsRepository,
    SnapshotRepository,
    SourceRepository,
    TaskRepository,
)
from .sessions import SessionRepository
from .models import ModelRepository
from .governance import GovernanceRepository
from .collaboration import CollaborationRepository
from .workflow import WorkflowRepository


@dataclass(frozen=True)
class WorkspaceRepositories:
    database: WorkspaceDatabase
    sessions: SessionRepository
    workflow: WorkflowRepository
    actions: ActionRepository
    tasks: TaskRepository
    sources: SourceRepository
    settings: SettingsRepository
    snapshots: SnapshotRepository
    models: ModelRepository
    governance: GovernanceRepository
    collaboration: CollaborationRepository

    @classmethod
    async def open(
        cls, path: str | Path, *, workspace: Path
    ) -> "WorkspaceRepositories":
        database = await WorkspaceDatabase.open(path)
        await database.execute(
            "INSERT OR IGNORE INTO database_metadata(key,value) VALUES('workspace',?)",
            (str(workspace.resolve()),),
        )
        collaboration = CollaborationRepository(database)
        await collaboration.interrupt_active()
        return cls(
            database,
            SessionRepository(database, workspace),
            WorkflowRepository(database),
            ActionRepository(database),
            TaskRepository(database),
            SourceRepository(database),
            SettingsRepository(database),
            SnapshotRepository(database),
            ModelRepository(database),
            GovernanceRepository(database),
            collaboration,
        )

    async def close(self) -> None:
        await self.database.close()


__all__ = [
    "ActionRepository",
    "SessionRepository",
    "ModelRepository",
    "GovernanceRepository",
    "CollaborationRepository",
    "SettingsRepository",
    "SnapshotRepository",
    "SourceRepository",
    "TaskRepository",
    "WorkflowRepository",
    "WorkspaceRepositories",
]
