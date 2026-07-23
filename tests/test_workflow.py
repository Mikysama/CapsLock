"""Workflow tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from capslock.domain import (
    ActionResultKind,
    ActionStatus,
    ActionType,
    AgentEventKind,
    WorkItemStatus,
    approval_outcome,
    interrupted_step_status,
    validate_work_item_transition,
)
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import workflow_service, workspace_run


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkItemStatus.QUEUED, WorkItemStatus.RUNNING),
        (WorkItemStatus.RUNNING, WorkItemStatus.WAITING_APPROVAL),
        (WorkItemStatus.WAITING_APPROVAL, WorkItemStatus.COMPLETED),
        (WorkItemStatus.COMPLETED, WorkItemStatus.COMPLETED),
    ],
)
def test_work_item_transition_policy_accepts_supported_edges(
    current: WorkItemStatus, target: WorkItemStatus
) -> None:
    validate_work_item_transition(current, target)


def test_work_item_transition_policy_rejects_terminal_reentry() -> None:
    with pytest.raises(ValueError, match="invalid work item transition"):
        validate_work_item_transition(WorkItemStatus.COMPLETED, WorkItemStatus.RUNNING)


def test_terminal_policy_maps_steps_and_approval_failures() -> None:
    assert interrupted_step_status(WorkItemStatus.COMPLETED) is None
    assert interrupted_step_status(WorkItemStatus.CANCELLED).value == "cancelled"
    completed = approval_outcome(None)
    assert completed.status is WorkItemStatus.COMPLETED
    failed = approval_outcome("failed", "nonzero_exit", "command failed")
    assert failed.status is WorkItemStatus.FAILED
    assert (failed.error_code, failed.error_message) == (
        "nonzero_exit",
        "command failed",
    )


def test_enqueue_start_and_compare_and_set_guards(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("model")
            workflow = workflow_service(repositories)
            first = await workflow.enqueue(session.id, "first")
            second = await workflow.enqueue(session.id, "second")
            assert (first.position, second.position) == (0, 1)
            prepared = await workflow.prepare(
                session.id, first.question, work_item_id=first.id
            )
            running = await repositories.work_items.require(first.id)
            assert running.status is WorkItemStatus.RUNNING
            assert running.current_run_id == prepared.run.id
            with pytest.raises(ValueError, match="not queued"):
                await workflow.prepare(
                    session.id, first.question, work_item_id=first.id
                )
            with pytest.raises(ValueError, match="only queued"):
                await repositories.work_items.reorder(first.id, 9)
        finally:
            await repositories.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "event_kind"),
    [
        (WorkItemStatus.COMPLETED, AgentEventKind.COMPLETED),
        (WorkItemStatus.FAILED, AgentEventKind.FAILED),
        (WorkItemStatus.CANCELLED, AgentEventKind.CANCELLED),
        (WorkItemStatus.WAITING_APPROVAL, AgentEventKind.WAITING_APPROVAL),
    ],
)
def test_atomic_terminal_transitions(
    tmp_path: Path, status: WorkItemStatus, event_kind: AgentEventKind
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / f"{status}.sqlite3", workspace=tmp_path
        )
        try:
            _, prepared = await workspace_run(repositories)
            event = await workflow_service(repositories).finish(
                prepared.run.id,
                status=status,
                event_kind=event_kind,
                payload={"status": status.value},
                duration_ms=7,
            )
            run = await repositories.runs.require(prepared.run.id)
            item = await repositories.work_items.require(prepared.work_item.id)
            assert run.status == item.status.value == status.value
            assert event.terminal
            assert [
                entry.sequence
                for entry in await repositories.run_journal.events(run.id)
            ] == [1]
            with pytest.raises(ValueError, match="not running"):
                await workflow_service(repositories).finish(
                    run.id,
                    status=status,
                    event_kind=event_kind,
                    payload={},
                    duration_ms=0,
                )
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_finalization_rolls_back_run_and_work_item_when_event_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            _, prepared = await workspace_run(repositories)

            async def fail(*args, **kwargs):
                raise RuntimeError("injected event failure")

            monkeypatch.setattr(repositories.workflow, "_append_event", fail)
            with pytest.raises(RuntimeError, match="injected"):
                await repositories.workflow.finalize(
                    prepared.run.id,
                    status=WorkItemStatus.COMPLETED,
                    event_kind=AgentEventKind.COMPLETED,
                    payload={},
                    duration_ms=1,
                )
            assert (
                await repositories.runs.require(prepared.run.id)
            ).status == "running"
            assert (
                await repositories.work_items.require(prepared.work_item.id)
            ).status is WorkItemStatus.RUNNING
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_approval_settlement_is_atomic_and_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            action = await repositories.actions.create(
                session_id=session.id,
                run_id=prepared.run.id,
                action_type=ActionType.FILE_EDIT,
                summary="proposal",
                request={"path": "README.md"},
            )
            await repositories.workflow.finalize(
                prepared.run.id,
                status=WorkItemStatus.WAITING_APPROVAL,
                event_kind=AgentEventKind.WAITING_APPROVAL,
                payload={"status": "waiting_approval", "action_ids": [action.id]},
                duration_ms=1,
            )
            assert (
                await repositories.workflow.settle_approval(session.id, prepared.run.id)
                is None
            )
            await repositories.actions.transition(action.id, ActionStatus.REJECTED)
            event = await repositories.workflow.settle_approval(
                session.id, prepared.run.id
            )
            assert event is not None and event.kind is AgentEventKind.COMPLETED
            assert (
                await repositories.workflow.settle_approval(session.id, prepared.run.id)
                is None
            )
            assert len(await repositories.run_journal.events(prepared.run.id)) == 2
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_approval_settlement_rolls_back_when_terminal_event_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            action = await repositories.actions.create(
                session_id=session.id,
                run_id=prepared.run.id,
                action_type=ActionType.FILE_CREATE,
                summary="proposal",
                request={"path": "new.txt"},
            )
            await repositories.workflow.finalize(
                prepared.run.id,
                status=WorkItemStatus.WAITING_APPROVAL,
                event_kind=AgentEventKind.WAITING_APPROVAL,
                payload={"status": "waiting_approval"},
                duration_ms=1,
            )
            await repositories.actions.transition(action.id, ActionStatus.REJECTED)

            async def fail(*args, **kwargs):
                raise RuntimeError("injected settlement failure")

            monkeypatch.setattr(repositories.workflow, "_append_event", fail)
            with pytest.raises(RuntimeError, match="injected settlement"):
                await repositories.workflow.settle_approval(session.id, prepared.run.id)
            assert (
                await repositories.runs.require(prepared.run.id)
            ).status == "waiting_approval"
            assert (
                await repositories.work_items.require(prepared.work_item.id)
            ).status is WorkItemStatus.WAITING_APPROVAL
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_failed_action_settles_workflow_as_failed(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            action = await repositories.actions.create(
                session_id=session.id,
                run_id=prepared.run.id,
                action_type=ActionType.COMMAND,
                summary="command",
                request={"argv": []},
            )
            await repositories.workflow.finalize(
                prepared.run.id,
                status=WorkItemStatus.WAITING_APPROVAL,
                event_kind=AgentEventKind.WAITING_APPROVAL,
                payload={"status": "waiting_approval"},
                duration_ms=1,
            )
            await repositories.actions.transition(action.id, ActionStatus.APPROVED)
            await repositories.actions.transition(action.id, ActionStatus.RUNNING)
            await repositories.actions.transition(
                action.id,
                ActionStatus.FAILED,
                result_kind=ActionResultKind.NONZERO_EXIT,
                error_code="nonzero_exit",
                error_message="command failed",
            )
            event = await repositories.workflow.settle_approval(
                session.id, prepared.run.id
            )
            assert event is not None and event.kind is AgentEventKind.FAILED
            assert (await repositories.runs.require(prepared.run.id)).status == "failed"
            assert event.data["error"]["code"] == "nonzero_exit"
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_waiting_action_cancellation_updates_action_run_and_work_item(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            action = await repositories.actions.create(
                session_id=session.id,
                run_id=prepared.run.id,
                action_type=ActionType.COMMAND,
                summary="long command",
                request={"argv": []},
            )
            await repositories.actions.transition(action.id, ActionStatus.APPROVED)
            await repositories.actions.transition(action.id, ActionStatus.RUNNING)
            await repositories.workflow.finalize(
                prepared.run.id,
                status=WorkItemStatus.WAITING_APPROVAL,
                event_kind=AgentEventKind.WAITING_APPROVAL,
                payload={"status": "waiting_approval"},
                duration_ms=1,
            )
            event = await repositories.workflow.cancel_waiting_action(
                session.id,
                prepared.run.id,
                action.id,
                message="cancelled by user",
            )
            assert event.kind is AgentEventKind.CANCELLED
            assert (
                await repositories.actions.require(action.id)
            ).status is ActionStatus.CANCELLED
            assert (
                await repositories.runs.require(prepared.run.id)
            ).status == "cancelled"
            assert (
                await repositories.work_items.require(prepared.work_item.id)
            ).status is WorkItemStatus.CANCELLED
        finally:
            await repositories.close()

    asyncio.run(scenario())
