"""Dependency-inversion contracts shared by the CapsLock core.

The protocols in this module deliberately describe behaviour rather than
concrete SQLite, CLI, SDK, or application-layer implementations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator, Protocol

from .domain import (
    ActionRecord,
    ActionStatus,
    ActionType,
    AgentEvent,
    AgentEventKind,
    ApprovalDecision,
    MemoryInfo,
    MemoryScope,
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


class WorkflowRepositoryPort(Protocol):
    async def enqueue(
        self, session_id: str, question: str, *, parent_work_item_id: str | None = None
    ) -> WorkItemInfo: ...

    async def retryable_run(self, session_id: str, prefix: str) -> RunInfo: ...

    async def last_stable_step(self, run_id: str) -> RunStepInfo | None: ...

    async def require_work_item(self, item_id: str) -> WorkItemInfo: ...

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

    async def settle_approval(
        self, session_id: str, run_id: str
    ) -> AgentEvent | None: ...


class ActionPort(Protocol):
    async def propose(
        self, action_type: ActionType, **payload: Any
    ) -> ActionRecord: ...

    async def resolve(
        self, prefix: str, *, types: set[ActionType] | None = None
    ) -> ActionRecord: ...

    def for_run(self, run_id: str) -> "ActionPort": ...

    async def approve_and_execute(self, action_id: str) -> ActionRecord: ...

    async def reject(self, action_id: str) -> ActionRecord: ...

    async def reverse_last_file_action(self) -> ActionRecord: ...


ActionAuthorizer = Callable[[ActionRecord], Awaitable[ApprovalDecision]]
ActionFactory = Callable[[str], ActionPort]


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


class ModelAuditPort(Protocol):
    async def record_decision(self, run_id: str, **values: Any) -> int: ...

    async def start_call(self, run_id: str, **values: Any) -> str: ...

    async def finish_call(self, call_id: str, **values: Any) -> None: ...

    async def record_budget(self, run_id: str, **values: Any) -> None: ...

    async def usage(self, run_id: str) -> tuple[int, int, float]: ...

    async def session_cost(self, run_id: str) -> float: ...

    async def summary(self, run_id: str) -> list[dict[str, Any]]: ...


class GovernancePort(Protocol):
    async def start(self, run_id: str, **values: Any) -> tuple[Any, list[Any]]: ...

    async def save(self, run_id: str, **values: Any) -> None: ...

    async def reserve_attempt(self, run_id: str, **values: Any) -> int: ...

    async def finish_attempt(self, attempt_id: int, **values: Any) -> None: ...


class CollaborationPort(Protocol):
    async def delegate(self, contracts: Any) -> list[Any]: ...

    def stream_status(self, task_ids: Any) -> AsyncIterator[dict[str, Any]]: ...

    async def wait(self, task_id: str) -> Any: ...

    async def validated_output(self, task_id: str) -> Any: ...

    async def cancel(self, task_id: str) -> None: ...

    async def cleanup(self, task_id: str) -> None: ...


class TaskPort(Protocol):
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


class MemoryPort(Protocol):
    async def search(
        self, query: str, *, run_id: str | None = None, limit: int = 10
    ) -> list[MemoryInfo]: ...

    async def get_for_model(self, prefix: str, *, run_id: str) -> MemoryInfo: ...

    async def recall_context(self, query: str, *, run_id: str) -> Any: ...

    async def capture_candidates(self, chat_model: Any, **kwargs: Any) -> Any: ...

    async def list(
        self,
        *,
        scope: MemoryScope | None = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> list[MemoryInfo]: ...


class SkillPort(Protocol):
    def load(self, run_id: str, name: str, *, trigger: str) -> Any: ...

    def load_data(
        self, run_id: str, name: str, *, trigger: str
    ) -> tuple[dict[str, Any], Any]: ...

    def read_resource(
        self, run_id: str, name: str, path: str, **kwargs: Any
    ) -> Any: ...

    def finish_run(self, run_id: str) -> None: ...


class SkillRegistryPort(Protocol):
    def catalog(self) -> Any: ...

    def entries(self) -> list[Any]: ...


class SessionPort(Protocol):
    async def append_message(
        self, session_id: str, run_id: str, role: str, content: str
    ) -> Any: ...

    async def require(self, session_id: str) -> Any: ...


class ActionRepositoryPort(Protocol):
    async def list(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        statuses: set[ActionStatus] | None = None,
    ) -> list[ActionRecord]: ...


class WorkspaceServicesPort(Protocol):
    sessions: SessionPort
    workflow: RunJournal
    actions: ActionRepositoryPort
    tasks: TaskPort
    sources: SourcePort
    models: ModelAuditPort
    governance: GovernancePort
    collaboration: Any


__all__ = [
    "ActionAuthorizer",
    "ActionFactory",
    "ActionPort",
    "CollaborationPort",
    "GovernancePort",
    "MemoryPort",
    "ModelAuditPort",
    "RunJournal",
    "SessionPort",
    "SkillPort",
    "SkillRegistryPort",
    "SourcePort",
    "TaskPort",
    "WorkflowPort",
    "WorkflowRepositoryPort",
    "WorkspaceServicesPort",
]
