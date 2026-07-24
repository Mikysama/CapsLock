"""Workflow, journal, session, task, and source ports."""

from __future__ import annotations

from typing import Any, Protocol

from ..domain import (
    AgentEvent,
    AgentEventKind,
    RunInfo,
    RunStepInfo,
    RunStepKind,
    RunStepStatus,
    SourceInfo,
    TaskInfo,
    WorkItemInfo,
    WorkItemStatus,
)


class WorkflowPort(Protocol):
    async def enqueue(
        self, session_id: str, question: str, *, parent_work_item_id: str | None = None
    ) -> WorkItemInfo: ...
    async def prepare(
        self,
        session_id: str,
        question: str,
        *,
        work_item_id: str | None = None,
        resume_from_run_id: str | None = None,
    ) -> Any: ...
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
    ) -> AgentEvent: ...
    async def settle_approval(
        self, session_id: str, run_id: str
    ) -> AgentEvent | None: ...
    async def pause(self, run_id: str, **values: Any) -> AgentEvent: ...
    async def resume_paused(self, session_id: str, run_id: str) -> None: ...


class WorkItemRepositoryPort(Protocol):
    async def enqueue(
        self, session_id: str, question: str, *, parent_work_item_id: str | None = None
    ) -> WorkItemInfo: ...
    async def require(self, item_id: str) -> WorkItemInfo: ...
    async def list(
        self, session_id: str, *, active_only: bool = False
    ) -> list[WorkItemInfo]: ...
    async def update(
        self, item_id: str, status: WorkItemStatus, *, error: str | None = None
    ) -> WorkItemInfo: ...
    async def reorder(self, item_id: str, position: int) -> WorkItemInfo: ...


class RunRepositoryPort(Protocol):
    async def get(
        self, run_id: str, *, session_id: str | None = None
    ) -> RunInfo | None: ...
    async def require(
        self, run_id: str, *, session_id: str | None = None
    ) -> RunInfo: ...
    async def retryable(self, session_id: str, prefix: str) -> RunInfo: ...
    async def completed(self, run_id: str) -> bool: ...
    async def session_cost(self, session_id: str) -> tuple[int, int, float]: ...


class RunJournalRepositoryPort(Protocol):
    async def last_stable_step(self, run_id: str) -> RunStepInfo | None: ...


class WorkflowUnitOfWorkPort(Protocol):
    async def start_run(
        self,
        session_id: str,
        work_item_id: str,
        question: str,
        *,
        parent_run_id: str | None = None,
        resume_from_step_id: str | None = None,
    ) -> RunInfo: ...
    async def finalize(self, run_id: str, **values: Any) -> AgentEvent: ...
    async def pause(self, run_id: str, **values: Any) -> AgentEvent: ...
    async def settle_approval(
        self, session_id: str, run_id: str
    ) -> AgentEvent | None: ...
    async def cancel_waiting_action(
        self, session_id: str, run_id: str, action_id: str, *, message: str
    ) -> AgentEvent: ...


class RunJournal(Protocol):
    async def get_run(self, run_id: str, **kwargs: Any) -> RunInfo | None: ...
    async def create_step(self, run_id: str, kind: RunStepKind) -> RunStepInfo: ...
    async def finish_step(
        self,
        step_id: str,
        *,
        status: RunStepStatus,
        checkpoint: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> RunStepInfo: ...
    async def append_event(
        self, run_id: str, kind: AgentEventKind, payload: dict[str, Any]
    ) -> AgentEvent: ...
    async def start_tool_invocation(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        name: str,
        spec: dict[str, Any],
        capabilities: dict[str, Any],
        arguments: dict[str, Any],
        status: str = "received",
    ) -> str: ...
    async def update_tool_invocation(
        self,
        identifier: str,
        *,
        status: str | None = None,
        policy: dict[str, Any] | None = None,
        timings: dict[str, int] | None = None,
    ) -> None: ...
    async def pause_tool_invocation(self, identifier: str, **values: Any) -> None: ...
    async def pause_step(self, identifier: str, **values: Any) -> None: ...
    async def update_step_checkpoint(
        self, identifier: str, checkpoint: dict[str, Any]
    ) -> None: ...
    async def create_input_request(self, **values: Any) -> None: ...
    async def finish_tool_invocation(
        self,
        identifier: str,
        *,
        status: str,
        execution_status: str,
        delivery_status: str,
        result_preview: str,
        duration_ms: int,
        artifact_id: str | None = None,
        error_code: str | None = None,
    ) -> None: ...
    async def record_tool_call(
        self,
        run_id: str,
        name: str,
        arguments: dict[str, Any],
        ok: bool,
        summary: str,
        duration_ms: int,
    ) -> None: ...
    async def record_citations(self, run_id: str, citations: list[Any]) -> None: ...


class TaskPort(Protocol):
    async def create(self, session_id: str, **values: Any) -> TaskInfo: ...
    async def list(self, session_id: str, **values: Any) -> list[TaskInfo]: ...
    async def get(
        self, task_id: str, *, session_id: str | None = None
    ) -> TaskInfo | None: ...
    async def update(self, task_id: str, session_id: str, **values: Any) -> TaskInfo: ...
    async def replace(
        self, session_id: str, items: list[str], *, run_id: str | None = None
    ) -> list[TaskInfo]: ...
    async def update_status(
        self, task_id: str, session_id: str, status: str
    ) -> TaskInfo: ...


class SourcePort(Protocol):
    async def list(self, session_id: str) -> list[SourceInfo]: ...
    async def get(self, source_id: str, **kwargs: Any) -> SourceInfo | None: ...
    async def add(self, **values: Any) -> SourceInfo: ...


class SessionPort(Protocol):
    async def append_message(
        self, session_id: str, run_id: str, role: str, content: str
    ) -> Any: ...
    async def require(self, session_id: str) -> Any: ...
    async def set_model(self, session_id: str, model: str) -> Any: ...
