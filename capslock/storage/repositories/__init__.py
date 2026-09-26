"""Composed async workspace repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from ..async_database import WorkspaceDatabase
from .actions import ActionRepository
from .misc import (
    SettingsRepository,
    SnapshotRepository,
    SourceRepository,
    TaskRepository,
)
from .collaboration import CollaborationRepository
from .compactions import ContextCompactionRepository
from .governance import GovernanceRepository
from .models import ModelRepository
from .plans import PlanRepository
from .performance import PerformanceRepository
from .episodic import EpisodicRepository
from .journal.repository import RunJournalRepository
from .runs import RunRepository
from .sessions import SessionRepository
from .work_items import WorkItemRepository
from .workflow_uow import WorkflowUnitOfWork


@dataclass(frozen=True)
class WorkspaceRepositories:
    database: WorkspaceDatabase
    sessions: SessionRepository
    work_items: WorkItemRepository
    runs: RunRepository
    run_journal: RunJournalRepository
    workflow: WorkflowUnitOfWork
    actions: ActionRepository
    tasks: TaskRepository
    sources: SourceRepository
    settings: SettingsRepository
    snapshots: SnapshotRepository
    models: ModelRepository
    governance: GovernanceRepository
    collaboration: CollaborationRepository
    compactions: ContextCompactionRepository
    plans: PlanRepository
    performance: PerformanceRepository
    episodic: EpisodicRepository
    project_instance_id: str

    @classmethod
    async def open(
        cls, path: str | Path, *, workspace: Path, shared_owner: bool = False
    ) -> "WorkspaceRepositories":
        database = await WorkspaceDatabase.open(path, shared_owner=shared_owner)
        try:
            return await cls._compose(database, workspace=workspace)
        except BaseException:
            await database.close()
            raise

    @classmethod
    async def _compose(
        cls, database: WorkspaceDatabase, *, workspace: Path
    ) -> "WorkspaceRepositories":
        await database.execute(
            "INSERT OR IGNORE INTO database_metadata(key,value) VALUES('workspace',?)",
            (str(workspace.resolve()),),
        )
        await database.execute(
            "INSERT OR IGNORE INTO database_metadata(key,value) VALUES('project_instance_id',?)",
            (uuid.uuid4().hex,),
        )
        project_row = await database.fetch_one(
            "SELECT value FROM database_metadata WHERE key='project_instance_id'"
        )
        assert project_row is not None
        collaboration = CollaborationRepository(database)
        if database.recovery_owner:
            await collaboration.interrupt_active()
        episodic = EpisodicRepository(database)
        journal = RunJournalRepository(database, episodic=episodic)
        if database.recovery_owner:
            await journal.interrupt_active()
        runs = RunRepository(database, journal)
        return cls(
            database,
            SessionRepository(database, workspace, episodic=episodic),
            WorkItemRepository(database),
            runs,
            journal,
            WorkflowUnitOfWork(database, runs, journal),
            ActionRepository(database),
            TaskRepository(database),
            SourceRepository(database),
            SettingsRepository(database),
            SnapshotRepository(database),
            ModelRepository(database),
            GovernanceRepository(database),
            collaboration,
            ContextCompactionRepository(database),
            PlanRepository(database),
            PerformanceRepository(database),
            episodic,
            str(project_row["value"]),
        )

    async def close(self) -> None:
        await self.database.close()


__all__ = [
    "ActionRepository",
    "SessionRepository",
    "ModelRepository",
    "PlanRepository",
    "PerformanceRepository",
    "EpisodicRepository",
    "GovernanceRepository",
    "CollaborationRepository",
    "ContextCompactionRepository",
    "SettingsRepository",
    "SnapshotRepository",
    "SourceRepository",
    "TaskRepository",
    "WorkItemRepository",
    "RunRepository",
    "RunJournalRepository",
    "WorkflowUnitOfWork",
    "WorkspaceRepositories",
]
