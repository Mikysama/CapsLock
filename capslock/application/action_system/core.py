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
    ApprovalDecision,
)
from ...interaction import RunInteraction
from ...permissions import ApprovalPolicy, PermissionMode
from ...storage.repositories_v2 import WorkspaceRepositories


@dataclass(frozen=True)
class ActionProposal:
    summary: str
    request: dict[str, Any]


@dataclass(frozen=True)
class ActionExecution:
    result: dict[str, Any]
    result_kind: ActionResultKind


class ActionHandler(Protocol):
    types: frozenset[ActionType]

    async def propose(
        self, action_type: ActionType, payload: dict[str, Any]
    ) -> ActionProposal: ...

    async def execute(self, action: ActionRecord) -> ActionExecution: ...

    async def reverse(self, action: ActionRecord) -> dict[str, Any]: ...


class ActionCoordinator:
    def __init__(
        self,
        repositories: WorkspaceRepositories,
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
    ) -> None:
        self.repositories = repositories
        self.session_id = session_id
        self.run_id = run_id
        self.event = event
        self.interaction = interaction or RunInteraction(
            permission_mode=permission_mode,
            action_authorizer=approval_authorizer,
        )
        self.approvals = ApprovalPolicy()
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
            self.repositories,
            session_id=self.session_id,
            run_id=run_id,
            handlers=list(dict.fromkeys(self.handlers.values())),
            event=self.event,
            interaction=self.interaction,
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
        proposal = await self.handlers[action_type].propose(action_type, payload)
        record = await self.repositories.actions.create(
            session_id=self.session_id,
            run_id=self.run_id,
            action_type=action_type,
            summary=proposal.summary,
            request=proposal.request,
        )
        assessment = self.approvals.assess(action_type)
        record = await self.repositories.actions.set_risk(
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
        requires_approval = self._skill_change(
            record
        ) or self.approvals.requires_approval(self.permission_mode, action_type)
        if requires_approval:
            if self.approval_authorizer is None:
                return record
            try:
                decision = ApprovalDecision(await self.approval_authorizer(record))
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(self.reject(record.id))
                await _await_cleanup(cleanup)
                raise
            except (EOFError, KeyboardInterrupt):
                decision = ApprovalDecision.REJECT
            if decision is ApprovalDecision.REJECT:
                return await self.reject(record.id)
            return await self.approve_and_execute(record.id)
        if self.permission_mode is PermissionMode.FULL_ACCESS:
            self.event(
                "auto_approved", action=action_type.value, level=assessment.level
            )
        return await self.approve_and_execute(record.id)

    async def resolve(
        self, prefix: str, *, types: set[ActionType] | None = None
    ) -> ActionRecord:
        return await self.repositories.actions.resolve(
            self.session_id, prefix, types=types
        )

    async def approve_and_execute(self, action_id: str) -> ActionRecord:
        action = await self.repositories.actions.require(
            action_id, session_id=self.session_id
        )
        if action.historical_only:
            raise ValueError("historical imported actions cannot be executed")
        if action.status is ActionStatus.PENDING and action.requires_reapproval:
            proposal = await self.handlers[action.type].propose(
                action.type, _reapproval_payload(action)
            )
            assessment = self.approvals.assess(action.type)
            action = await self.repositories.actions.mark_revalidated(
                action.id,
                summary=proposal.summary,
                request=proposal.request,
                level=assessment.level,
                reason=assessment.reason,
                rollback=assessment.rollback,
            )
        if action.status is ActionStatus.PENDING:
            action = await self.repositories.actions.transition(
                action.id, ActionStatus.APPROVED
            )
        if action.status is not ActionStatus.APPROVED:
            raise ValueError("action requires approval before execution")
        return await self.execute_approved(action.id)

    async def execute_approved(self, action_id: str) -> ActionRecord:
        try:
            action = await self.repositories.actions.require(
                action_id, session_id=self.session_id
            )
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._record_cancellation(action_id))
            await _await_cleanup(cleanup)
            raise
        if action.status is not ActionStatus.APPROVED:
            raise ValueError("action requires explicit approval before execution")
        try:
            action = await self.repositories.actions.transition(
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
            return await self.repositories.actions.transition(
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
        return await self.repositories.actions.transition(
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
        action = await self.repositories.actions.require(
            action_id, session_id=self.session_id
        )
        if action.status in {
            ActionStatus.COMPLETED,
            ActionStatus.FAILED,
            ActionStatus.CANCELLED,
            ActionStatus.REJECTED,
        }:
            return
        run = await self.repositories.workflow.require_run(action.run_id)
        if run.status == "waiting_approval":
            await self.repositories.workflow.cancel_waiting_action(
                self.session_id,
                action.run_id,
                action.id,
                message="cancelled by user",
            )
        else:
            await self.repositories.actions.transition(
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
        action = await self.repositories.actions.require(
            action_id, session_id=self.session_id
        )
        return await self.repositories.actions.transition(
            action.id, ActionStatus.REJECTED
        )

    async def reverse_last_file_action(self) -> ActionRecord:
        action = await self.repositories.actions.last_completed_file_action(
            self.session_id
        )
        if action is None:
            raise ValueError("no applied change is available to undo")
        await self.handlers[action.type].reverse(action)
        return await self.repositories.actions.mark_reversed(action.id)

    @staticmethod
    def _skill_change(action: ActionRecord) -> bool:
        if action.type not in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
            return False
        path = str(action.request.get("path", ""))
        parts = path.split("/")
        return len(parts) >= 3 and tuple(parts[:2]) == (".capslock", "skills")


def _reapproval_payload(action: ActionRecord) -> dict[str, Any]:
    request = action.request
    payload = {**request, "summary": action.summary}
    if action.type is ActionType.FILE_CREATE:
        return {
            "path": request.get("path"),
            "content": request.get("after_content"),
            "summary": action.summary,
        }
    if action.type is ActionType.FILE_EDIT:
        return {
            "path": request.get("path"),
            "old_text": request.get("before_content"),
            "new_text": request.get("after_content"),
            "summary": action.summary,
        }
    if action.type is ActionType.COMMAND:
        from .commands import TEMPLATES

        template_name = str(request.get("template", ""))
        template = TEMPLATES.get(template_name)
        argv = request.get("argv", [])
        if template is None or not isinstance(argv, list):
            return payload
        expected = len(template.argv)
        target = (
            argv[expected]
            if template.supports_target and len(argv) == expected + 1
            else None
        )
        return {
            "template": template_name,
            "target": target,
            "cwd": request.get("cwd", "."),
        }
    return payload


async def _await_cleanup(task: asyncio.Task) -> None:
    """Finish cancellation cleanup before propagating even under repeated cancel()."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    await task
