"""Async action handler contract and coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...domain import (
    ActionRecord,
    ActionResultKind,
    ActionStatus,
    ActionType,
    AgentEvent,
    ApprovalDecision,
    ApprovalChoice,
)
from ...interaction import RunInteraction
from ...permissions import ApprovalPolicy, PermissionMode
from ...ports import (
    ActionRepositoryPort,
    RunRepositoryPort,
    RunStatePort,
    WaitingActionPort,
)


@dataclass(frozen=True)
class ActionProposal:
    summary: str
    request: dict[str, Any]


@dataclass(frozen=True)
class ActionExecution:
    result: dict[str, Any]
    result_kind: ActionResultKind


class ActionRunState:
    """Adapt workflow query and transition ports for action cancellation."""

    def __init__(
        self,
        runs: RunRepositoryPort,
        waiting_actions: WaitingActionPort,
    ) -> None:
        self.runs = runs
        self.waiting_actions = waiting_actions

    async def require(self, run_id: str, **values: Any):
        return await self.runs.require(run_id, **values)

    async def cancel_waiting_action(
        self,
        session_id: str,
        run_id: str,
        action_id: str,
        *,
        message: str,
    ) -> AgentEvent:
        return await self.waiting_actions.cancel_waiting_action(
            session_id, run_id, action_id, message=message
        )


class ActionHandler(Protocol):
    types: frozenset[ActionType]

    async def propose(
        self, action_type: ActionType, payload: dict[str, Any]
    ) -> ActionProposal: ...

    async def execute(self, action: ActionRecord) -> ActionExecution: ...

    async def revalidate(self, action: ActionRecord) -> ActionProposal: ...

    async def reverse(self, action: ActionRecord) -> dict[str, Any]: ...


class ActionCoordinator:
    def __init__(
        self,
        action_repository: ActionRepositoryPort,
        run_state: RunStatePort,
        *,
        session_id: str,
        run_id: str,
        handlers: list[ActionHandler],
        event: Callable[..., None],
        permission_mode: PermissionMode = PermissionMode.APPROVE_FOR_ME,
        approval_authorizer: (
            Callable[[ActionRecord], Awaitable[ApprovalDecision]] | None
        ) = None,
        interaction: RunInteraction | None = None,
        permission_engine: Any = None,
    ) -> None:
        self.action_repository = action_repository
        self.run_state = run_state
        self.session_id = session_id
        self.run_id = run_id
        self.event = event
        self.interaction = interaction or RunInteraction(
            permission_mode=permission_mode,
            action_authorizer=approval_authorizer,
        )
        self.approvals = ApprovalPolicy()
        self.permission_engine = permission_engine
        self.handlers = {
            action_type: handler
            for handler in handlers
            for action_type in handler.types
        }
        missing = set(ActionType) - set(self.handlers)
        if missing:
            raise ValueError(
                f"missing action handlers: {', '.join(sorted(item.value for item in missing))}"
            )

    def for_run(self, run_id: str) -> "ActionCoordinator":
        return ActionCoordinator(
            self.action_repository,
            self.run_state,
            session_id=self.session_id,
            run_id=run_id,
            handlers=list(dict.fromkeys(self.handlers.values())),
            event=self.event,
            interaction=self.interaction,
            permission_engine=self.permission_engine,
        )

    @property
    def permission_mode(self) -> PermissionMode:
        return self.interaction.permission_mode

    @permission_mode.setter
    def permission_mode(self, value: PermissionMode) -> None:
        self.interaction.permission_mode = value

    @property
    def approval_authorizer(
        self,
    ) -> Callable[[ActionRecord], Awaitable[ApprovalDecision]] | None:
        return self.interaction.action_authorizer

    @approval_authorizer.setter
    def approval_authorizer(
        self,
        value: Callable[[ActionRecord], Awaitable[ApprovalDecision]] | None,
    ) -> None:
        self.interaction.action_authorizer = value

    async def propose(self, action_type: ActionType, **payload: Any) -> ActionRecord:
        permission = payload.pop("_permission", None)
        proposal = await self.handlers[action_type].propose(action_type, payload)
        request = dict(proposal.request)
        if isinstance(permission, dict):
            request["_permission"] = permission
        record = await self.action_repository.create(
            session_id=self.session_id,
            run_id=self.run_id,
            action_type=action_type,
            summary=proposal.summary,
            request=request,
        )
        assessment = self.approvals.assess(action_type)
        record = await self.action_repository.set_risk(
            record.id,
            level=assessment.level,
            reason=assessment.reason,
            rollback=assessment.rollback,
        )
        self.event(
            "risk_assessed",
            action=action_type.value,
            level=assessment.level,
            rollback=assessment.rollback,
        )
        requires_approval = (
            action_type
            in {
                ActionType.WORKTREE_EXIT,
                ActionType.SESSION_REWIND,
                ActionType.CREDENTIAL_ACCESS,
            }
            or record.request.get("force_manual_approval") is True
            or self._skill_change(record)
            or (
                not _permission_preapproved(record.request)
                and self.approvals.requires_approval(self.permission_mode, action_type)
            )
        )
        if requires_approval:
            if self.approval_authorizer is None:
                return record
            try:
                decision = _approval_choice(await self.approval_authorizer(record))
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(self.reject(record.id))
                await _await_cleanup(cleanup)
                raise
            except (EOFError, KeyboardInterrupt):
                decision = ApprovalChoice.REJECT
            if decision is ApprovalChoice.REJECT:
                return await self.reject(record.id)
            if decision in {
                ApprovalChoice.APPROVE_SESSION,
                ApprovalChoice.APPROVE_LOCAL,
            }:
                await self._persist_permission_choice(record, decision)
            return await self.approve_and_execute(record.id)
        if self.permission_mode is PermissionMode.FULL_ACCESS:
            self.event(
                "auto_approved", action=action_type.value, level=assessment.level
            )
        return await self.approve_and_execute(record.id)

    async def _persist_permission_choice(
        self, record: ActionRecord, choice: ApprovalChoice
    ) -> None:
        if self.permission_engine is None:
            raise ValueError("permission persistence is unavailable")
        permission = record.request.get("_permission")
        suggestions = permission.get("suggestions") if isinstance(permission, dict) else None
        destination = (
            "session" if choice is ApprovalChoice.APPROVE_SESSION else "local"
        )
        selected = next(
            (
                item
                for item in suggestions or []
                if isinstance(item, dict) and item.get("destination") == destination
            ),
            None,
        )
        if selected is None:
            raise ValueError(f"no {destination} permission suggestion is available")
        from ...tooling.authorization import (
            PermissionBehavior,
            PermissionDestination,
            PermissionUpdate,
            PermissionUpdateOperation,
        )

        update = PermissionUpdate(
            PermissionUpdateOperation(str(selected["operation"])),
            PermissionDestination(str(selected["destination"])),
            PermissionBehavior(str(selected["behavior"])),
            str(selected["tool"]),
            dict(selected.get("constraints", {})),
            str(selected["rule_id"]) if selected.get("rule_id") else None,
        )
        await self.permission_engine.apply_update(self.session_id, update)
        allowed = await self.permission_engine.verify_explicit_allow(
            session_id=self.session_id,
            tool=update.tool,
            arguments=_permission_arguments(record.request),
        )
        if not allowed:
            raise ValueError(
                "persisted permission is shadowed and cannot authorize this action"
            )

    async def resolve(
        self, prefix: str, *, types: set[ActionType] | None = None
    ) -> ActionRecord:
        return await self.action_repository.resolve(
            self.session_id, prefix, types=types
        )

    async def approve_and_execute(self, action_id: str) -> ActionRecord:
        action = await self.action_repository.require(
            action_id, session_id=self.session_id
        )
        if action.historical_only:
            raise ValueError("historical imported actions cannot be executed")
        if action.status is ActionStatus.PENDING and action.requires_reapproval:
            proposal = await self.handlers[action.type].revalidate(action)
            assessment = self.approvals.assess(action.type)
            action = await self.action_repository.mark_revalidated(
                action.id,
                summary=proposal.summary,
                request=proposal.request,
                level=assessment.level,
                reason=assessment.reason,
                rollback=assessment.rollback,
            )
        if action.status is ActionStatus.PENDING:
            action = await self.action_repository.transition(
                action.id, ActionStatus.APPROVED
            )
        if action.status is not ActionStatus.APPROVED:
            raise ValueError("action requires approval before execution")
        return await self.execute_approved(action.id)

    async def approve_with_choice(
        self, action_id: str, choice: ApprovalChoice | ApprovalDecision | str
    ) -> ActionRecord:
        selected = _approval_choice(choice)
        if selected is ApprovalChoice.REJECT:
            return await self.reject(action_id)
        action = await self.action_repository.require(
            action_id, session_id=self.session_id
        )
        if selected in {
            ApprovalChoice.APPROVE_SESSION,
            ApprovalChoice.APPROVE_LOCAL,
        }:
            await self._persist_permission_choice(action, selected)
        return await self.approve_and_execute(action_id)

    async def execute_approved(self, action_id: str) -> ActionRecord:
        try:
            action = await self.action_repository.require(
                action_id, session_id=self.session_id
            )
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._record_cancellation(action_id))
            await _await_cleanup(cleanup)
            raise
        if action.status is not ActionStatus.APPROVED:
            raise ValueError("action requires explicit approval before execution")
        try:
            action = await self.action_repository.transition(
                action.id, ActionStatus.RUNNING
            )
            execution = await self.handlers[action.type].execute(action)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._record_cancellation(action.id))
            await _await_cleanup(cleanup)
            raise
        except Exception as exc:
            self.event(
                "action_finished",
                action_id=action.id,
                action=action.type.value,
                status="failed",
            )
            return await self.action_repository.transition(
                action.id,
                ActionStatus.FAILED,
                result_kind=ActionResultKind.EXECUTION_ERROR,
                error_code=type(exc).__name__,
                error_message=str(exc) or type(exc).__name__,
            )
        self.event(
            "action_finished",
            action_id=action.id,
            action=action.type.value,
            status=(
                "failed"
                if execution.result_kind
                in {ActionResultKind.NONZERO_EXIT, ActionResultKind.TIMEOUT}
                else "completed"
            ),
        )
        target = (
            ActionStatus.FAILED
            if execution.result_kind
            in {ActionResultKind.NONZERO_EXIT, ActionResultKind.TIMEOUT}
            else ActionStatus.COMPLETED
        )
        return await self.action_repository.transition(
            action.id,
            target,
            result=execution.result,
            result_kind=execution.result_kind,
            error_code=execution.result_kind.value
            if target is ActionStatus.FAILED
            else None,
            error_message=(
                "command timed out"
                if execution.result_kind is ActionResultKind.TIMEOUT
                else "command exited with a non-zero status"
                if execution.result_kind is ActionResultKind.NONZERO_EXIT
                else None
            ),
        )

    async def _record_cancellation(self, action_id: str) -> None:
        action = await self.action_repository.require(
            action_id, session_id=self.session_id
        )
        if action.status in {
            ActionStatus.COMPLETED,
            ActionStatus.FAILED,
            ActionStatus.CANCELLED,
            ActionStatus.REJECTED,
        }:
            return
        run = await self.run_state.require(action.run_id)
        if run.status == "waiting_approval":
            await self.run_state.cancel_waiting_action(
                self.session_id,
                action.run_id,
                action.id,
                message="cancelled by user",
            )
        else:
            await self.action_repository.transition(
                action.id,
                ActionStatus.CANCELLED,
                result_kind=ActionResultKind.USER_CANCELLED,
                error_code="cancelled",
                error_message="cancelled by user",
            )
        self.event(
            "action_finished",
            action_id=action.id,
            action=action.type.value,
            status="cancelled",
        )

    async def reject(self, action_id: str) -> ActionRecord:
        action = await self.action_repository.require(
            action_id, session_id=self.session_id
        )
        return await self.action_repository.transition(action.id, ActionStatus.REJECTED)

    async def reverse_last_file_action(self) -> ActionRecord:
        action = await self.action_repository.last_completed_file_action(
            self.session_id
        )
        if action is None:
            raise ValueError("no applied change is available to undo")
        await self.handlers[action.type].reverse(action)
        return await self.action_repository.mark_reversed(action.id)

    @staticmethod
    def _skill_change(action: ActionRecord) -> bool:
        if action.type not in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
            return False
        path = str(action.request.get("path", ""))
        parts = path.split("/")
        return len(parts) >= 3 and tuple(parts[:2]) == (".capslock", "skills")


async def _await_cleanup(task: asyncio.Task) -> None:
    """Finish cancellation cleanup before propagating even under repeated cancel()."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    await task


def _permission_preapproved(request: dict[str, Any]) -> bool:
    permission = request.get("_permission")
    return isinstance(permission, dict) and permission.get("behavior") == "allow"


def _permission_arguments(request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in request.items()
        if key not in {"_permission", "force_manual_approval", "argv", "temporary", "safety"}
    }


def _approval_choice(value: object) -> ApprovalChoice:
    if value in {ApprovalDecision.APPROVE, ApprovalDecision.APPROVE.value}:
        return ApprovalChoice.APPROVE_ONCE
    if value in {ApprovalDecision.REJECT, ApprovalDecision.REJECT.value}:
        return ApprovalChoice.REJECT
    return ApprovalChoice(value)
