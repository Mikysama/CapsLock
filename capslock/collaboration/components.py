"""Mailbox, artifact publication, and audit components for collaboration."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    AgentMessage,
    AgentMessageKind,
    AgentTaskContract,
    MailboxMessageKind,
)
from .workspace import AgentWorkspaceManager, WorkspaceSnapshot


class CollaborationMailbox:
    def __init__(
        self,
        *,
        repository,
        enabled: bool,
        ttl_seconds: int,
        active_states: set[str],
        cancel: Callable[[str], Awaitable[None]],
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.repository = repository
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.active_states = active_states
        self._cancel = cancel
        self._notify = notify

    async def send_message(
        self,
        task_id: str,
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
        kind: MailboxMessageKind,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("agent mailbox is disabled")
        task = await self.repository.get_task(task_id)
        if task is None or not _owns(
            task, parent_run_id=parent_run_id, session_id=session_id
        ):
            raise ValueError("child task does not belong to this controller")
        if str(task["state"]) not in self.active_states:
            raise ValueError(
                "mailbox messages can only be sent to an active child task"
            )
        if kind not in {
            MailboxMessageKind.INSTRUCTION,
            MailboxMessageKind.RESPONSE,
            MailboxMessageKind.CANCEL,
        }:
            raise ValueError("parent cannot send this mailbox message kind")
        if kind is MailboxMessageKind.CANCEL:
            await self._cancel(task_id)
        message = await self.repository.send_mailbox(
            task_id=task_id,
            parent_run_id=str(task["parent_run_id"]),
            sender="parent",
            recipient="child",
            kind=kind,
            payload=payload,
            ttl_seconds=self.ttl_seconds,
        )
        if self._notify is not None:
            await self._notify(str(task_id))
        return message

    async def read_messages(
        self,
        task_id: str,
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            raise ValueError("agent mailbox is disabled")
        task = await self.repository.get_task(task_id)
        if task is None or not _owns(
            task, parent_run_id=parent_run_id, session_id=session_id
        ):
            raise ValueError("child task does not belong to this controller")
        return await self.repository.receive_mailbox(task_id, recipient="parent")

    async def acknowledge_message(
        self,
        message_id: str,
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if not self.enabled:
            raise ValueError("agent mailbox is disabled")
        message = await self.repository.mailbox_message(message_id)
        task = (
            None
            if message is None
            else await self.repository.get_task(str(message["task_id"]))
        )
        if task is None or not _owns(
            task, parent_run_id=parent_run_id, session_id=session_id
        ):
            raise ValueError("mailbox message does not belong to this controller")
        await self.repository.acknowledge_mailbox(message_id, recipient="parent")

    async def send_child_message(
        self,
        task_id: str,
        *,
        parent_run_id: str,
        kind: MailboxMessageKind,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("agent mailbox is disabled")
        task = await self.repository.get_task(task_id)
        if task is None or str(task["parent_run_id"]) != parent_run_id:
            raise ValueError("child task does not belong to this run")
        if str(task["state"]) not in self.active_states:
            raise ValueError("only an active child task can send mailbox messages")
        if kind not in {
            MailboxMessageKind.QUESTION,
            MailboxMessageKind.RESPONSE,
            MailboxMessageKind.PROGRESS,
            MailboxMessageKind.ARTIFACT_OFFER,
        }:
            raise ValueError("child cannot send this mailbox message kind")
        message = await self.repository.send_mailbox(
            task_id=task_id,
            parent_run_id=parent_run_id,
            sender="child",
            recipient="parent",
            kind=kind,
            payload=payload,
            ttl_seconds=self.ttl_seconds,
        )
        if self._notify is not None:
            await self._notify(str(task_id))
        return message

    async def read_child_messages(
        self, task_id: str, *, parent_run_id: str
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            raise ValueError("agent mailbox is disabled")
        task = await self.repository.get_task(task_id)
        if task is None or str(task["parent_run_id"]) != parent_run_id:
            raise ValueError("child task does not belong to this run")
        worker_id = task.get("assigned_worker_id")
        if worker_id:
            return await self.repository.receive_worker_mailbox(str(worker_id))
        return await self.repository.receive_mailbox(task_id, recipient="child")

    async def acknowledge_child_message(
        self, message_id: str, *, task_id: str, parent_run_id: str
    ) -> None:
        if not self.enabled:
            raise ValueError("agent mailbox is disabled")
        message = await self.repository.mailbox_message(message_id)
        task = await self.repository.get_task(task_id)
        message_task = (
            None
            if message is None
            else await self.repository.get_task(str(message["task_id"]))
        )
        same_worker = bool(
            task
            and message_task
            and task.get("assigned_worker_id")
            and task.get("assigned_worker_id") == message_task.get("assigned_worker_id")
        )
        if (
            message is None
            or task is None
            or (str(message["task_id"]) != task_id and not same_worker)
            or str(task["parent_run_id"]) != parent_run_id
        ):
            raise ValueError("mailbox message does not belong to this child task")
        await self.repository.acknowledge_mailbox(message_id, recipient="child")


class CollaborationArtifactPublisher:
    def __init__(self, *, repository, workspace_manager: AgentWorkspaceManager) -> None:
        self.repository = repository
        self.workspace_manager = workspace_manager

    async def publish_artifact(
        self,
        task_id: str,
        artifact: dict[str, Any],
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        task = await self.repository.get_task(task_id)
        if task is None or not _owns(
            task, parent_run_id=parent_run_id, session_id=session_id
        ):
            raise ValueError("child task does not belong to this controller")
        contract = AgentTaskContract.from_dict(json.loads(str(task["contract_json"])))
        snapshot = await self._snapshot_for(task_id)
        if not snapshot.root.is_dir():
            raise ValueError("child workspace is no longer available")
        source = snapshot.resolve(
            str(artifact.get("path", "")), allowed_paths=contract.allowed_paths
        )
        if not source.is_file():
            raise ValueError("child artifact is not a regular file")
        if (
            source.stat().st_size
            > contract.verification_requirements.max_artifact_bytes
        ):
            raise ValueError("child artifact exceeds the contract size limit")
        self.workspace_manager.publish_artifacts(
            snapshot, (artifact,), allowed_paths=contract.allowed_paths
        )
        await self.repository.update_workspace_baseline(
            str(snapshot.root), self.workspace_manager.baseline(snapshot)
        )

    async def _snapshot_for(self, task_id: str) -> WorkspaceSnapshot:
        row = await self.repository.one(
            """SELECT path,workspace_mode,base_commit,baseline_json
               FROM agent_workspaces WHERE task_id=?""",
            (task_id,),
        )
        if row is None:
            raise ValueError(f"child workspace does not exist: {task_id}")
        snapshot = WorkspaceSnapshot(
            self.workspace_manager.parent_workspace,
            Path(str(row["path"])),
            str(row["workspace_mode"]),
            row["base_commit"],
        )
        self.workspace_manager.restore_baseline(
            snapshot, json.loads(str(row["baseline_json"]))
        )
        return snapshot


class CollaborationAudit:
    def __init__(self, *, repository) -> None:
        self.repository = repository

    async def audit_approval(
        self,
        contract: AgentTaskContract,
        *,
        decided: bool,
        payload: dict[str, Any],
    ) -> None:
        await self._audit(
            contract,
            AgentMessageKind.APPROVAL_DECIDED
            if decided
            else AgentMessageKind.APPROVAL_REQUESTED,
            payload,
        )

    async def _audit(
        self,
        contract: AgentTaskContract,
        kind: AgentMessageKind,
        payload: dict[str, Any],
    ) -> None:
        await self._audit_by_id(
            contract.task_id, kind, payload, parent_run_id=contract.parent_run_id
        )

    async def _audit_by_id(
        self,
        task_id: str,
        kind: AgentMessageKind,
        payload: dict[str, Any],
        *,
        parent_run_id: str | None = None,
    ) -> None:
        if parent_run_id is None:
            row = await self.repository.one(
                "SELECT parent_run_id FROM agent_tasks WHERE id=?", (task_id,)
            )
            if row is None:
                return
            parent_run_id = str(row[0])
        row = await self.repository.one(
            "SELECT coalesce(max(sequence),0)+1 FROM agent_messages WHERE task_id=?",
            (task_id,),
        )
        sequence = int(row[0])
        message = AgentMessage(
            message_id=f"msg_{task_id}_{sequence}",
            task_id=task_id,
            parent_run_id=parent_run_id,
            sender=("child" if kind is AgentMessageKind.MESSAGE_RECEIVED else "parent"),
            recipient=(
                "parent" if kind is AgentMessageKind.MESSAGE_RECEIVED else "child"
            ),
            sequence=sequence,
            kind=kind,
            payload=payload,
            created_at=datetime.now(UTC).isoformat(),
        )
        await self.repository.record_message(message)


def _owns(
    task: dict[str, Any], *, parent_run_id: str | None, session_id: str | None
) -> bool:
    if session_id is not None:
        return str(task.get("owner_session_id", "")) == session_id
    if parent_run_id is not None:
        return str(task.get("parent_run_id", "")) == parent_run_id
    raise ValueError("agent task ownership scope is required")


__all__ = [
    "CollaborationArtifactPublisher",
    "CollaborationAudit",
    "CollaborationMailbox",
]
