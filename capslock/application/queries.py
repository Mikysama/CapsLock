"""Read-only workspace query surface exposed by the composition root."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain import (
    ActionRecord,
    ActionStatus,
    ActionType,
    AgentEvent,
    BudgetSnapshot,
    RunInfo,
    SessionInfo,
    SourceInfo,
    TaskInfo,
    WorkItemInfo,
)
from ..collaboration.models import ValidatedAgentOutput
from ..storage.repositories.actions import ActionRepository
from ..storage.repositories.collaboration import CollaborationRepository
from ..storage.repositories.governance import GovernanceRepository
from ..storage.repositories.misc import SourceRepository, TaskRepository
from ..storage.repositories.run_journal import RunJournalRepository
from ..storage.repositories.runs import RunRepository
from ..storage.repositories.sessions import SessionRepository
from ..storage.repositories.work_items import WorkItemRepository


@dataclass(frozen=True)
class WorkspaceQueries:
    _sessions: SessionRepository
    _runs: RunRepository
    _work_items: WorkItemRepository
    _journal: RunJournalRepository
    _actions: ActionRepository
    _tasks: TaskRepository
    _sources: SourceRepository
    _governance: GovernanceRepository
    _collaboration: CollaborationRepository

    async def session(self, session_id: str) -> SessionInfo:
        return await self._sessions.require(session_id)

    async def sessions(
        self, *, limit: int = 20, include_archived: bool = False
    ) -> list[SessionInfo]:
        return await self._sessions.list(limit, include_archived=include_archived)

    async def transcript(self, session_id: str) -> list[dict[str, object]]:
        return await self._sessions.transcript(session_id)

    async def run(self, run_id: str, *, session_id: str | None = None) -> RunInfo:
        return await self._runs.require(run_id, session_id=session_id)

    async def events(self, run_id: str) -> list[AgentEvent]:
        return await self._journal.events(run_id)

    async def work_items(
        self, session_id: str, *, active_only: bool = False
    ) -> list[WorkItemInfo]:
        return await self._work_items.list(session_id, active_only=active_only)

    async def actions(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        types: set[ActionType] | None = None,
        statuses: set[ActionStatus] | None = None,
    ) -> list[ActionRecord]:
        return await self._actions.list(
            session_id,
            run_id=run_id,
            types=types,
            statuses=statuses,
        )

    async def tasks(self, session_id: str) -> list[TaskInfo]:
        return await self._tasks.list(session_id)

    async def sources(self, session_id: str) -> list[SourceInfo]:
        return await self._sources.list(session_id)

    async def session_cost(self, session_id: str) -> tuple[int, int, float]:
        return await self._runs.session_cost(session_id)

    async def message_count(self, session_id: str) -> int:
        return await self._sessions.message_count(session_id)

    async def latest_budget(self, session_id: str) -> BudgetSnapshot | None:
        return await self._governance.latest_for_session(session_id)

    async def collaboration_tasks(self, session_id: str) -> list[dict[str, Any]]:
        return await self._collaboration.list_for_session(session_id)

    async def collaboration_run_tasks(self, run_id: str) -> list[dict[str, Any]]:
        return await self._collaboration.list_tasks(run_id)

    async def collaboration_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._collaboration.get_task(task_id)

    async def collaboration_output(
        self, task_id: str
    ) -> ValidatedAgentOutput | None:
        return await self._collaboration.get_output(task_id)

    async def collaboration_messages(self, task_id: str) -> list[dict[str, Any]]:
        return await self._collaboration.messages(task_id)

    async def collaboration_workspace(self, task_id: str) -> dict[str, Any] | None:
        return await self._collaboration.workspace(task_id)
