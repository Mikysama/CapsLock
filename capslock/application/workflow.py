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
from ..ports import (
    RunJournalRepositoryPort,
    RunRepositoryPort,
    WorkItemRepositoryPort,
    WorkflowUnitOfWorkPort,
)


@dataclass(frozen=True)
class PreparedRun:
    work_item: WorkItemInfo
    run: RunInfo
    checkpoint: RunStepInfo | None = None
    resumed: bool = False


class WorkflowService:
    def __init__(
        self,
        work_items: WorkItemRepositoryPort,
        runs: RunRepositoryPort,
        journal: RunJournalRepositoryPort,
        unit_of_work: WorkflowUnitOfWorkPort,
    ) -> None:
        self.work_items = work_items
        self.runs = runs
        self.journal = journal
        self.unit_of_work = unit_of_work

    async def enqueue(
        self, session_id: str, question: str, *, parent_work_item_id: str | None = None
    ) -> WorkItemInfo:
        normalized = question.strip()
        if not normalized:
            raise ValueError("question must not be empty")
        return await self.work_items.enqueue(
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
            candidate = await self.runs.get(
                resume_from_run_id, session_id=session_id
            )
            if candidate is not None and candidate.status in {
                "waiting_approval",
                "waiting_input",
            }:
                await self.unit_of_work.resume_paused(session_id, candidate.id)
                resumed_run = await self.runs.require(
                    candidate.id, session_id=session_id
                )
                checkpoint = await self.journal.last_stable_step(candidate.id)
                if checkpoint is None:
                    raise ValueError("paused run has no resumable checkpoint")
                resumed_item = await self.work_items.require(candidate.work_item_id)
                return PreparedRun(resumed_item, resumed_run, checkpoint, True)
            parent = await self.runs.retryable(session_id, resume_from_run_id)
            checkpoint = await self.journal.last_stable_step(parent.id)
            assert checkpoint is not None
            parent_work_item_id = parent.work_item_id
        if work_item_id is None:
            work_item = await self.enqueue(
                session_id, question, parent_work_item_id=parent_work_item_id
            )
        else:
            work_item = await self.work_items.require(work_item_id)
            if (
                work_item.session_id != session_id
                or work_item.status is not WorkItemStatus.QUEUED
            ):
                raise ValueError("work item is not queued in this session")
        run = await self.unit_of_work.start_run(
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
        return await self.unit_of_work.finalize(
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
        return await self.unit_of_work.settle_approval(session_id, run_id)

    async def pause(
        self, run_id: str, *, kind: str, payload: dict[str, Any]
    ) -> AgentEvent:
        return await self.unit_of_work.pause(run_id, kind=kind, payload=payload)
