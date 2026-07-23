"""Action coordination ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..domain import (
    ActionRecord,
    ActionStatus,
    ActionType,
    AgentEvent,
    ApprovalDecision,
    RunInfo,
)


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


class ActionRepositoryPort(Protocol):
    async def create(self, **values: Any) -> ActionRecord: ...
    async def require(self, action_id: str, **values: Any) -> ActionRecord: ...
    async def resolve(
        self, session_id: str, prefix: str, *, types: set[ActionType] | None = None
    ) -> ActionRecord: ...
    async def set_risk(self, action_id: str, **values: Any) -> ActionRecord: ...
    async def mark_revalidated(self, action_id: str, **values: Any) -> ActionRecord: ...
    async def transition(
        self, action_id: str, status: ActionStatus, **values: Any
    ) -> ActionRecord: ...
    async def last_completed_file_action(
        self, session_id: str
    ) -> ActionRecord | None: ...
    async def mark_reversed(self, action_id: str) -> ActionRecord: ...
    async def list(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        statuses: set[ActionStatus] | None = None,
    ) -> list[ActionRecord]: ...


class RunStatePort(Protocol):
    async def require(self, run_id: str, **values: Any) -> RunInfo: ...


class WaitingActionPort(Protocol):
    async def cancel_waiting_action(
        self, session_id: str, run_id: str, action_id: str, *, message: str
    ) -> AgentEvent: ...
