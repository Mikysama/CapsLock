"""Application service for atomic foreground workflow transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain import (
    AgentEvent,
    AgentEventKind,
    RunInfo,
    RunStepInfo,
    WorkItemInfo,
    WorkItemStatus,
)
from ..storage.repositories_v2 import WorkspaceRepositories


@dataclass(frozen=True)
class PreparedRun:
    work_item: WorkItemInfo
    run: RunInfo
    checkpoint: RunStepInfo | None = None


class WorkflowService:
    def __init__(self, repositories: WorkspaceRepositories) -> None:
        self.repositories = repositories

    async def enqueue(
        self, session_id: str, question: str, *, parent_work_item_id: str | None = None
    ) -> WorkItemInfo:
        normalized = question.strip()
        if not normalized:
            raise ValueError("question must not be empty")
        return await self.repositories.workflow.enqueue(
            session_id, normalized, parent_work_item_id=parent_work_item_id
        )

    async def prepare(
        self,
        session_id: str,
        question: str,
        *,
        work_item_id: str | None = None,
        resume_from_run_id: str | None = None,
    ) -> PreparedRun:
        checkpoint = None
        parent_work_item_id = None
        if resume_from_run_id is not None:
            parent = await self.repositories.workflow.retryable_run(
                session_id, resume_from_run_id
            )
            checkpoint = await self.repositories.workflow.last_stable_step(parent.id)
            assert checkpoint is not None
            parent_work_item_id = parent.work_item_id
        if work_item_id is None:
            work_item = await self.enqueue(
                session_id, question, parent_work_item_id=parent_work_item_id
            )
        else:
            work_item = await self.repositories.workflow.require_work_item(work_item_id)
            if (
                work_item.session_id != session_id
                or work_item.status is not WorkItemStatus.QUEUED
            ):
                raise ValueError("work item is not queued in this session")
        run = await self.repositories.workflow.start_run(
            session_id,
            work_item.id,
            work_item.question,
            parent_run_id=resume_from_run_id,
            resume_from_step_id=checkpoint.id if checkpoint else None,
        )
        return PreparedRun(work_item, run, checkpoint)

    async def finish(
        self,
        run_id: str,
        *,
        status: WorkItemStatus,
        event_kind: AgentEventKind,
        payload: dict[str, Any],
        duration_ms: int,
        error_code: str | None = None,
        error_message: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0,
        stop_reason: str | None = None,
    ) -> AgentEvent:
        return await self.repositories.workflow.finalize(
            run_id,
            status=status,
            event_kind=event_kind,
            payload=payload,
            duration_ms=duration_ms,
            error_code=error_code,
            error_message=error_message,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            stop_reason=stop_reason,
        )

    async def settle_approval(self, session_id: str, run_id: str) -> AgentEvent | None:
        return await self.repositories.workflow.settle_approval(session_id, run_id)
