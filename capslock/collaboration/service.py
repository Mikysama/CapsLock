"""Bounded local parent/child task scheduler."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from pathlib import Path

from .models import (
    AgentMessage,
    AgentMessageKind,
    AgentTaskContract,
    AgentTaskState,
    ValidatedAgentOutput,
)
from .verifier import AgentOutputVerifier, VerificationError
from .workspace import AgentWorkspaceManager, WorkspaceSnapshot


ChildRunner = Callable[
    [AgentTaskContract, WorkspaceSnapshot], Awaitable[dict[str, Any]]
]


class ChildApprovalPending(RuntimeError):
    """A non-interactive child stopped with independently pending actions."""


class CollaborationService:
    def __init__(
        self,
        *,
        workspace_manager: AgentWorkspaceManager,
        repository: Any,
        max_children: int = 4,
        max_concurrency: int = 2,
        max_depth: int = 1,
        child_runner: ChildRunner | None = None,
        verifier: AgentOutputVerifier | None = None,
    ) -> None:
        if max_children < 1 or max_concurrency < 1:
            raise ValueError("collaboration limits must be positive")
        if max_depth != 1:
            raise ValueError("only one child delegation level is supported")
        self.workspace_manager = workspace_manager
        self.repository = repository
        self.max_children = max_children
        self.max_concurrency = min(max_concurrency, max_children)
        self.max_depth = max_depth
        self.child_runner = child_runner
        self.verifier = verifier or AgentOutputVerifier()
        self._tasks: dict[str, asyncio.Task[ValidatedAgentOutput]] = {}
        self._contracts: dict[str, AgentTaskContract] = {}

    async def delegate(
        self, contracts: Sequence[AgentTaskContract]
    ) -> list[ValidatedAgentOutput]:
        if not contracts:
            raise ValueError("at least one child task is required")
        if len(contracts) > self.max_children:
            raise ValueError(
                f"at most {self.max_children} child tasks may be delegated"
            )
        if any(
            contract.parent_run_id != contracts[0].parent_run_id
            for contract in contracts
        ):
            raise ValueError("all child tasks must belong to the same parent run")
        identifiers = [contract.task_id for contract in contracts]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("child task ids must be unique")
        if self.child_runner is None:
            raise RuntimeError("child Agent runner is not configured")
        for contract in contracts:
            parent = await self.repository.one(
                "SELECT status FROM runs WHERE id=?", (contract.parent_run_id,)
            )
            if parent is None or str(parent["status"]) != "running":
                raise ValueError("parent run must exist and be running")
            if contract.task_id in self._tasks:
                raise ValueError(f"child task already exists: {contract.task_id}")
            snapshot = self.workspace_manager.create(contract.task_id)
            try:
                await self.repository.create_task(
                    contract,
                    workspace_path=str(snapshot.root),
                    source_path=str(snapshot.source),
                )
            except BaseException:
                self.workspace_manager.cleanup(snapshot)
                raise
            await self._audit(
                contract,
                AgentMessageKind.TASK_CREATED,
                {"contract_sha256": contract.digest()},
            )
        semaphore = asyncio.Semaphore(self.max_concurrency)
        started = {contract.task_id: asyncio.Event() for contract in contracts}

        async def run(contract: AgentTaskContract) -> ValidatedAgentOutput:
            started[contract.task_id].set()
            try:
                async with semaphore:
                    return await self._run_one(contract)
            except asyncio.CancelledError:
                task = await self.repository.get_task(contract.task_id)
                if task is not None and task["state"] == AgentTaskState.CREATED.value:
                    return await self._record_cancelled(contract, "cancelled")
                raise

        tasks = [asyncio.create_task(run(contract)) for contract in contracts]
        await asyncio.gather(*(event.wait() for event in started.values()))
        for contract, task in zip(contracts, tasks, strict=True):
            self._tasks[contract.task_id] = task
            self._contracts[contract.task_id] = contract
        try:
            return await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            for contract in contracts:
                self._tasks.pop(contract.task_id, None)
                self._contracts.pop(contract.task_id, None)

    async def cancel(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None and not task.done():
            contract = self._contracts.get(task_id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            current = await self.repository.get_task(task_id)
            if current is not None and current["state"] == AgentTaskState.CREATED.value:
                if contract is not None:
                    await self._record_cancelled(contract, "cancelled by user")
            return
        current = await self.repository.get_task(task_id)
        if current is None:
            raise ValueError("child task does not exist")
        if current["state"] not in {
            AgentTaskState.CREATED.value,
            AgentTaskState.RUNNING.value,
            AgentTaskState.WAITING_APPROVAL.value,
        }:
            raise ValueError("only active child tasks can be cancelled")
        await self.repository.set_state(
            task_id, AgentTaskState.CANCELLED, error="cancelled by user"
        )
        await self._audit_by_id(
            task_id, AgentMessageKind.TASK_CANCELLED, {"reason": "cancelled by user"}
        )

    async def stream_status(
        self, task_ids: Sequence[str]
    ) -> AsyncIterator[dict[str, Any]]:
        if not task_ids:
            raise ValueError("at least one child task id is required")
        previous: dict[str, str] = {}
        terminal = {
            AgentTaskState.COMPLETED.value,
            AgentTaskState.FAILED.value,
            AgentTaskState.CANCELLED.value,
            AgentTaskState.INTERRUPTED.value,
        }
        while True:
            snapshot: dict[str, str] = {}
            for task_id in task_ids:
                task = await self.repository.get_task(task_id)
                if task is None:
                    raise ValueError(f"child task does not exist: {task_id}")
                snapshot[task_id] = str(task["state"])
            if snapshot != previous:
                yield {"tasks": dict(snapshot)}
                previous = snapshot
            if all(state in terminal for state in snapshot.values()):
                return
            await asyncio.sleep(0.05)

    async def wait(self, task_id: str) -> ValidatedAgentOutput:
        task = self._tasks.get(task_id)
        if task is not None:
            return await asyncio.shield(task)
        output = await self.repository.get_output(task_id)
        if output is None:
            record = await self.repository.get_task(task_id)
            if record is None:
                raise ValueError("child task does not exist")
            raise ValueError("child task has not produced a terminal output")
        return output

    async def validated_output(self, task_id: str) -> ValidatedAgentOutput:
        output = await self.wait(task_id)
        if not output.verified:
            raise ValueError(output.error or "child output was not verified")
        return output

    async def cleanup(self, task_id: str) -> None:
        task = await self.repository.get_task(task_id)
        if task is None:
            raise ValueError("child task does not exist")
        if task["state"] in {"created", "running", "waiting_approval"}:
            raise ValueError("active child workspace cannot be cleaned")
        snapshot = await self._snapshot_for(task_id)
        if snapshot.root.exists():
            self.workspace_manager.cleanup(snapshot)
        await self.repository.mark_cleaned(task_id)

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

    async def _run_one(self, contract: AgentTaskContract) -> ValidatedAgentOutput:
        snapshot = await self._snapshot_for(contract.task_id)
        await self.repository.set_state(contract.task_id, AgentTaskState.RUNNING)
        await self._audit(
            contract, AgentMessageKind.TASK_STARTED, {"isolated_workspace": True}
        )
        await self._audit(
            contract,
            AgentMessageKind.MESSAGE_SENT,
            {"objective": contract.objective, "input_context": contract.input_context},
        )
        try:
            assert self.child_runner is not None
            raw = await self.child_runner(contract, snapshot)
            child_run_id = raw.get("_child_run_id")
            if child_run_id is not None:
                if not isinstance(child_run_id, str) or not child_run_id:
                    raise VerificationError("child run id is invalid")
                await self.repository.set_state(
                    contract.task_id,
                    AgentTaskState.RUNNING,
                    child_run_id=child_run_id,
                )
            await self._audit(
                contract, AgentMessageKind.MESSAGE_RECEIVED, {"output": raw}
            )
            verified = self.verifier.verify(contract, snapshot, raw)
        except asyncio.CancelledError:
            return await self._record_cancelled(contract, "cancelled")
        except ChildApprovalPending as exc:
            current = await self.repository.get_task(contract.task_id)
            if current is not None and current["state"] == AgentTaskState.RUNNING.value:
                await self.repository.set_state(
                    contract.task_id, AgentTaskState.WAITING_APPROVAL
                )
            await self.repository.mark_retained(contract.task_id)
            return ValidatedAgentOutput(
                task_id=contract.task_id,
                state=AgentTaskState.WAITING_APPROVAL,
                summary="",
                verified=False,
                error=str(exc),
            )
        except Exception as exc:
            verified = self.verifier.rejected(contract, str(exc))
            await self.repository.set_state(
                contract.task_id, AgentTaskState.FAILED, error=str(exc)
            )
            await self.repository.record_output(verified)
            await self._audit(
                contract, AgentMessageKind.OUTPUT_REJECTED, {"error": str(exc)}
            )
            await self._audit(
                contract,
                AgentMessageKind.TASK_FINISHED,
                {"state": AgentTaskState.FAILED.value},
            )
            await self.repository.mark_retained(contract.task_id)
            return verified
        try:
            self.workspace_manager.publish_artifacts(
                snapshot,
                verified.artifacts,
                allowed_paths=contract.allowed_paths,
            )
        except (OSError, ValueError) as exc:
            rejected = self.verifier.rejected(contract, str(exc))
            await self.repository.set_state(
                contract.task_id, AgentTaskState.FAILED, error=str(exc)
            )
            await self.repository.record_output(rejected)
            await self._audit(
                contract, AgentMessageKind.OUTPUT_REJECTED, {"error": str(exc)}
            )
            await self._audit(
                contract,
                AgentMessageKind.TASK_FINISHED,
                {"state": AgentTaskState.FAILED.value},
            )
            await self.repository.mark_retained(contract.task_id)
            return rejected
        await self.repository.set_state(contract.task_id, AgentTaskState.COMPLETED)
        await self.repository.record_output(verified)
        await self._audit(
            contract,
            AgentMessageKind.OUTPUT_VERIFIED,
            {
                "output_sha256": hashlib.sha256(
                    json.dumps(
                        verified.as_dict(), sort_keys=True, ensure_ascii=False
                    ).encode("utf-8")
                ).hexdigest()
            },
        )
        await self._audit(
            contract,
            AgentMessageKind.TASK_FINISHED,
            {"state": AgentTaskState.COMPLETED.value},
        )
        if snapshot.root.exists():
            try:
                self.workspace_manager.cleanup(snapshot)
            except OSError:
                await self.repository.mark_retained(contract.task_id)
            else:
                await self.repository.mark_cleaned(contract.task_id)
        return verified

    async def _record_cancelled(
        self, contract: AgentTaskContract, reason: str
    ) -> ValidatedAgentOutput:
        current = await self.repository.get_task(contract.task_id)
        if current is not None and current["state"] in {
            AgentTaskState.CREATED.value,
            AgentTaskState.RUNNING.value,
            AgentTaskState.WAITING_APPROVAL.value,
        }:
            await self.repository.set_state(
                contract.task_id, AgentTaskState.CANCELLED, error=reason
            )
        output = ValidatedAgentOutput(
            task_id=contract.task_id,
            state=AgentTaskState.CANCELLED,
            summary="",
            verified=False,
            error=reason,
        )
        if await self.repository.get_output(contract.task_id) is None:
            await self.repository.record_output(output)
        await self._audit(contract, AgentMessageKind.TASK_CANCELLED, {"reason": reason})
        await self._audit(
            contract,
            AgentMessageKind.TASK_FINISHED,
            {"state": AgentTaskState.CANCELLED.value},
        )
        await self.repository.mark_retained(contract.task_id)
        return output

    async def _snapshot_for(self, task_id: str) -> WorkspaceSnapshot:
        row = await self.repository.one(
            "SELECT path FROM agent_workspaces WHERE task_id=?", (task_id,)
        )
        if row is None:
            raise ValueError(f"child workspace does not exist: {task_id}")
        return WorkspaceSnapshot(
            self.workspace_manager.parent_workspace, Path(str(row["path"]))
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
