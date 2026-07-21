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

    @classmethod
    async def open(
        cls, path: str | Path, *, workspace: Path
    ) -> "WorkspaceRepositories":
        database = await WorkspaceDatabase.open(path)
        await database.execute(
            "INSERT OR IGNORE INTO database_metadata(key,value) VALUES('workspace',?)",
            (str(workspace.resolve()),),
        )
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
        )

    async def close(self) -> None:
        await self.database.close()


__all__ = [
    "ActionRepository",
    "SessionRepository",
    "ModelRepository",
    "GovernanceRepository",
    "SettingsRepository",
    "SnapshotRepository",
    "SourceRepository",
    "TaskRepository",
    "WorkflowRepository",
    "WorkspaceRepositories",
]
