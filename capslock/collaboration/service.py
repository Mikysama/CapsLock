"""Bounded local parent/child task scheduler."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from ..behavior_defaults import (
    DEFAULT_AGENT_MAX_CHILDREN,
    DEFAULT_AGENT_MAX_CONCURRENCY,
    DEFAULT_AGENT_MAX_DEPTH,
)
from .components import (
    CollaborationArtifactPublisher,
    CollaborationAudit,
    CollaborationMailbox,
)
from .models import (
    AgentMessageKind,
    AgentTaskContract,
    AgentTaskState,
    MailboxMessageKind,
    ValidatedAgentOutput,
    WorkspaceMode,
)
from .verifier import AgentOutputVerifier, VerificationError
from .workspace import AgentWorkspaceManager, WorkspaceSnapshot

ChildRunner = Callable[
    [AgentTaskContract, WorkspaceSnapshot], Awaitable[dict[str, Any]]
]


class ChildApprovalPending(RuntimeError):
    """A non-interactive child stopped with independently pending actions."""


class CollaborationService:
    _ACTIVE_STATES = {
        AgentTaskState.CREATED.value,
        AgentTaskState.READY.value,
        AgentTaskState.CLAIMED.value,
        AgentTaskState.RUNNING.value,
        AgentTaskState.WAITING_APPROVAL.value,
    }

    def __init__(
        self,
        *,
        workspace_manager: AgentWorkspaceManager,
        repository: Any,
        max_children: int = DEFAULT_AGENT_MAX_CHILDREN,
        max_concurrency: int = DEFAULT_AGENT_MAX_CONCURRENCY,
        max_depth: int = DEFAULT_AGENT_MAX_DEPTH,
        child_runner: ChildRunner | None = None,
        verifier: AgentOutputVerifier | None = None,
        background_enabled: bool = True,
        mailbox_enabled: bool = True,
        message_ttl_seconds: int = 3600,
        default_workspace_mode: str = "snapshot",
        proposal_handler: Callable[
            [AgentTaskContract, ValidatedAgentOutput], Awaitable[None]
        ]
        | None = None,
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
        self.background_enabled = background_enabled
        self.mailbox_enabled = mailbox_enabled
        self.message_ttl_seconds = message_ttl_seconds
        self.default_workspace_mode = WorkspaceMode(default_workspace_mode)
        self.proposal_handler = proposal_handler
        self._tasks: dict[str, asyncio.Task[ValidatedAgentOutput]] = {}
        self._contracts: dict[str, AgentTaskContract] = {}
        self._attempts: dict[str, str] = {}
        self._workers: dict[str, str] = {}
        # Admission is owned by the workspace service, not by an individual
        # delegate() call.  This is the actual max-concurrency boundary.
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._admission_lock = asyncio.Lock()
        self._mailbox = CollaborationMailbox(
            repository=repository,
            enabled=mailbox_enabled,
            ttl_seconds=message_ttl_seconds,
            active_states=set(self._ACTIVE_STATES),
            cancel=self.cancel,
        )
        self._artifact_publisher = CollaborationArtifactPublisher(
            repository=repository,
            workspace_manager=workspace_manager,
        )
        self._audit_log = CollaborationAudit(repository=repository)

    async def delegate(
        self,
        contracts: Sequence[AgentTaskContract],
        *,
        background: bool = False,
    ) -> list[ValidatedAgentOutput]:
        if not contracts:
            raise ValueError("at least one child task is required")
        if background and not self.background_enabled:
            raise ValueError("background child Agents are disabled")
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
        async with self._admission_lock:
            parent = await self.repository.one(
                "SELECT status,session_id FROM runs WHERE id=?",
                (contracts[0].parent_run_id,),
            )
            if parent is None or str(parent["status"]) != "running":
                raise ValueError("parent run must exist and be running")
            session_id = str(parent["session_id"])
            team_id = await self.repository.ensure_default_team(
                session_id, created_by_run_id=contracts[0].parent_run_id
            )
            live_row = await self.repository.one(
                "SELECT count(*) FROM agent_workers WHERE team_id=? AND state<>'stopped'",
                (team_id,),
            )
            live_workers = int(live_row[0]) if live_row is not None else 0
            if live_workers + len(contracts) > self.max_children:
                raise ValueError(
                    f"team already has {live_workers} live child Agents; "
                    f"at most {self.max_children} may be live"
                )
            created: list[tuple[AgentTaskContract, WorkspaceSnapshot]] = []
            try:
                for contract in contracts:
                    parent_for_contract = await self.repository.one(
                        "SELECT status,session_id FROM runs WHERE id=?",
                        (contract.parent_run_id,),
                    )
                    if (
                        parent_for_contract is None
                        or str(parent_for_contract["status"]) != "running"
                        or str(parent_for_contract["session_id"]) != session_id
                    ):
                        raise ValueError(
                            "parent run must exist in the owning session and be running"
                        )
                    if contract.task_id in self._tasks:
                        raise ValueError(
                            f"child task already exists: {contract.task_id}"
                        )
                    worker = await self.repository.create_worker(
                        team_id,
                        f"delegate-{contract.task_id[:12]}",
                        profile={"contract_sha256": contract.digest()},
                        persistent=False,
                    )
                    worker_id = str(worker["id"])
                    snapshot = self.workspace_manager.create(contract.task_id)
                    created.append((contract, snapshot))
                    await self.repository.create_task(
                        contract,
                        workspace_path=str(snapshot.root),
                        source_path=str(snapshot.source),
                        baseline=self.workspace_manager.baseline(snapshot),
                        team_id=team_id,
                        worker_id=worker_id,
                    )
                    claim = await self.repository.claim_task(
                        contract.task_id,
                        worker_id,
                        reservation=dict(contract.limits),
                        account_usage=background,
                    )
                    self._attempts[contract.task_id] = str(claim["attempt_id"])
                    self._workers[contract.task_id] = worker_id
                    await self._audit(
                        contract,
                        AgentMessageKind.TASK_CREATED,
                        {"contract_sha256": contract.digest(), "team_id": team_id},
                    )
            except BaseException:
                for _, snapshot in created:
                    if snapshot.root.exists():
                        self.workspace_manager.cleanup(snapshot)
                raise
        started = {contract.task_id: asyncio.Event() for contract in contracts}

        async def run(contract: AgentTaskContract) -> ValidatedAgentOutput:
            started[contract.task_id].set()
            try:
                async with self._semaphore:
                    return await self._run_one(contract)
            except asyncio.CancelledError:
                task = await self.repository.get_task(contract.task_id)
                if task is not None and task["state"] in {
                    AgentTaskState.CREATED.value,
                    AgentTaskState.READY.value,
                    AgentTaskState.CLAIMED.value,
                }:
                    return await self._record_cancelled(contract, "cancelled")
                raise

        tasks = [asyncio.create_task(run(contract)) for contract in contracts]
        await asyncio.gather(*(event.wait() for event in started.values()))
        for contract, task in zip(contracts, tasks, strict=True):
            self._tasks[contract.task_id] = task
            self._contracts[contract.task_id] = contract
        if background:
            for contract, task in zip(contracts, tasks, strict=True):
                task.add_done_callback(
                    lambda completed, task_id=contract.task_id: self._background_done(
                        task_id, completed
                    )
                )
            return [
                ValidatedAgentOutput(
                    task_id=contract.task_id,
                    state=AgentTaskState.CREATED,
                    summary="background child Agent started",
                )
                for contract in contracts
            ]
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
                self._attempts.pop(contract.task_id, None)
                self._workers.pop(contract.task_id, None)

    def _background_done(
        self, task_id: str, task: asyncio.Task[ValidatedAgentOutput]
    ) -> None:
        self._workers.pop(task_id, None)
        self._tasks.pop(task_id, None)
        self._contracts.pop(task_id, None)
        self._attempts.pop(task_id, None)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass

    async def _run_next_for_worker(self, worker_id: str) -> None:
        worker = await self.repository.worker(worker_id)
        if (
            worker is None
            or not bool(worker.get("persistent"))
            or str(worker["state"]) != "idle"
        ):
            return
        task = await self.repository.one(
            """SELECT id,owner_session_id FROM agent_tasks
               WHERE assigned_worker_id=? AND state IN ('created','ready')
               ORDER BY priority DESC,created_at,id LIMIT 1""",
            (worker_id,),
        )
        if task is None:
            team = await self.repository.one(
                """SELECT t.id,t.session_id FROM agent_teams t
                   JOIN agent_workers w ON w.team_id=t.id WHERE w.id=?""",
                (worker_id,),
            )
            if team is not None:
                await self._schedule_team(
                    str(team["id"]), session_id=str(team["session_id"])
                )
            return
        try:
            await self.assign_agent_task(
                str(task["id"]),
                worker_id,
                session_id=str(task["owner_session_id"]),
            )
        except (RuntimeError, ValueError):
            # Dependency-blocked tasks remain queued and visible to the controller.
            team = await self.repository.one(
                """SELECT t.id,t.session_id FROM agent_teams t
                   JOIN agent_workers w ON w.team_id=t.id WHERE w.id=?""",
                (worker_id,),
            )
            if team is not None:
                await self._schedule_team(
                    str(team["id"]), session_id=str(team["session_id"])
                )
            return

    async def _schedule_team(self, team_id: str, *, session_id: str) -> None:
        """Event-driven ready-task assignment; no mailbox polling is involved."""
        team = await self.repository.team(team_id, session_id=session_id)
        idle = [item for item in team["workers"] if str(item["state"]) == "idle"]
        queued = [
            item for item in team["tasks"] if str(item["state"]) in {"created", "ready"}
        ]
        for task in queued:
            candidates = [
                worker
                for worker in idle
                if task.get("assigned_worker_id") is None
                or str(worker["id"]) == str(task["assigned_worker_id"])
            ]
            for worker in candidates:
                try:
                    await self.assign_agent_task(
                        str(task["id"]),
                        str(worker["id"]),
                        session_id=session_id,
                    )
                except (RuntimeError, ValueError):
                    continue
                idle.remove(worker)
                break
            if not idle:
                return

    async def status(
        self,
        task_id: str,
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
        wait: bool = False,
        timeout: float = 60,
    ) -> dict[str, Any]:
        record = await self.repository.get_task(task_id)
        if record is None:
            raise ValueError("child task does not exist")
        if parent_run_id is not None and str(record["parent_run_id"]) != parent_run_id:
            raise ValueError("child task does not belong to this run")
        if session_id is not None:
            if str(record.get("owner_session_id")) != session_id:
                raise ValueError("child task does not belong to this session")
        if parent_run_id is None and session_id is None:
            raise ValueError("child task ownership scope is required")
        if wait and task_id in self._tasks:
            try:
                async with asyncio.timeout(min(max(timeout, 0.1), 60)):
                    await asyncio.shield(self._tasks[task_id])
            except TimeoutError:
                pass
            record = await self.repository.get_task(task_id)
            assert record is not None
        output = await self.repository.get_output(task_id)
        return {
            "task_id": task_id,
            "state": str(record["state"]),
            "error": record.get("error"),
            "child_run_id": record.get("child_run_id"),
            "output": output.as_dict() if output is not None else None,
        }

    async def cancel(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None and not task.done():
            contract = self._contracts.get(task_id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            current = await self.repository.get_task(task_id)
            if current is not None and current["state"] in {
                AgentTaskState.CREATED.value,
                AgentTaskState.READY.value,
                AgentTaskState.CLAIMED.value,
            }:
                if contract is not None:
                    await self._record_cancelled(contract, "cancelled by user")
            return
        current = await self.repository.get_task(task_id)
        if current is None:
            raise ValueError("child task does not exist")
        if current["state"] not in {
            AgentTaskState.CREATED.value,
            AgentTaskState.READY.value,
            AgentTaskState.CLAIMED.value,
            AgentTaskState.RUNNING.value,
            AgentTaskState.WAITING_APPROVAL.value,
        }:
            raise ValueError("only active child tasks can be cancelled")
        contract = AgentTaskContract.from_dict(
            json.loads(str(current["contract_json"]))
        )
        cancel_suspended = getattr(self.child_runner, "cancel_suspended", None)
        if cancel_suspended is not None:
            await cancel_suspended(task_id)
        await self._record_cancelled(contract, "cancelled by user")

    async def send_message(
        self,
        task_id: str,
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
        kind: MailboxMessageKind,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._mailbox.send_message(
            task_id,
            parent_run_id=parent_run_id,
            session_id=session_id,
            kind=kind,
            payload=payload,
        )

    async def read_messages(
        self,
        task_id: str,
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._mailbox.read_messages(
            task_id, parent_run_id=parent_run_id, session_id=session_id
        )

    async def acknowledge_message(
        self,
        message_id: str,
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        return await self._mailbox.acknowledge_message(
            message_id, parent_run_id=parent_run_id, session_id=session_id
        )

    async def send_child_message(
        self,
        task_id: str,
        *,
        parent_run_id: str,
        kind: MailboxMessageKind,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._mailbox.send_child_message(
            task_id, parent_run_id=parent_run_id, kind=kind, payload=payload
        )

    async def read_child_messages(
        self, task_id: str, *, parent_run_id: str
    ) -> list[dict[str, Any]]:
        return await self._mailbox.read_child_messages(
            task_id, parent_run_id=parent_run_id
        )

    async def acknowledge_child_message(
        self, message_id: str, *, task_id: str, parent_run_id: str
    ) -> None:
        return await self._mailbox.acknowledge_child_message(
            message_id, task_id=task_id, parent_run_id=parent_run_id
        )

    async def publish_artifact(
        self,
        task_id: str,
        artifact: dict[str, Any],
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        return await self._artifact_publisher.publish_artifact(
            task_id,
            artifact,
            parent_run_id=parent_run_id,
            session_id=session_id,
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

    async def create_team(
        self, session_id: str, name: str, *, created_by_run_id: str | None = None
    ) -> dict[str, Any]:
        return await self.repository.create_team(
            session_id, name, created_by_run_id=created_by_run_id
        )

    async def start_agent(
        self,
        *,
        session_id: str,
        team_id: str,
        name: str,
        profile: dict[str, Any] | None = None,
        workspace_mode: WorkspaceMode | str | None = None,
    ) -> dict[str, Any]:
        mode = (
            self.default_workspace_mode
            if workspace_mode is None
            else WorkspaceMode(str(workspace_mode))
        )
        async with self._admission_lock:
            team = await self.repository.team(team_id, session_id=session_id)
            live = [item for item in team["workers"] if str(item["state"]) != "stopped"]
            if len(live) >= self.max_children:
                raise ValueError("agent team has reached max_children")
            return await self.repository.create_worker(
                team_id,
                name,
                profile=profile,
                workspace_mode=mode.value,
                persistent=True,
            )

    async def create_agent_task(
        self,
        contract: AgentTaskContract,
        *,
        session_id: str,
        team_id: str,
        depends_on: tuple[str, ...] = (),
        worker_id: str | None = None,
        plan_task_id: str | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        parent = await self.repository.one(
            "SELECT session_id,status FROM runs WHERE id=?", (contract.parent_run_id,)
        )
        if parent is None or str(parent["session_id"]) != session_id:
            raise ValueError("task creator run does not belong to this session")
        await self.repository.create_task(
            contract,
            team_id=team_id,
            worker_id=worker_id,
            plan_task_id=plan_task_id,
            priority=priority,
            depends_on=depends_on,
        )
        await self._audit(
            contract,
            AgentMessageKind.TASK_CREATED,
            {"contract_sha256": contract.digest(), "team_id": team_id},
        )
        task = await self.repository.get_task(contract.task_id)
        assert task is not None
        if worker_id is None:
            await self._schedule_team(team_id, session_id=session_id)
            task = await self.repository.get_task(contract.task_id)
            assert task is not None
        return task

    async def assign_agent_task(
        self, task_id: str, worker_id: str, *, session_id: str
    ) -> dict[str, Any]:
        if not await self.repository.owns_task(task_id, session_id):
            raise ValueError("agent task does not belong to this session")
        if self.child_runner is None:
            raise RuntimeError("child Agent runner is not configured")
        task = await self.repository.get_task(task_id)
        assert task is not None
        contract = AgentTaskContract.from_dict(json.loads(str(task["contract_json"])))
        if contract.digest() != str(task["contract_sha256"]):
            raise ValueError("agent task contract digest does not match")
        worker = await self.repository.worker(worker_id)
        if worker is None or str(worker["state"]) == "stopped":
            raise ValueError("active agent worker does not exist")
        self._validate_worker_profile(worker, contract)
        workspace = await self.repository.worker_workspace(worker_id)
        if workspace is None:
            snapshot = (
                self.workspace_manager.create_worktree(worker_id)
                if str(worker["workspace_mode"]) == WorkspaceMode.WORKTREE.value
                else (
                    self.workspace_manager.shared_read()
                    if str(worker["workspace_mode"]) == WorkspaceMode.SHARED_READ.value
                    else self.workspace_manager.create(worker_id)
                )
            )
            await self.repository.attach_workspace(
                task_id,
                worker_id=worker_id,
                mode=str(worker["workspace_mode"]),
                path=str(snapshot.root),
                source_path=str(snapshot.source),
                base_commit=snapshot.base_commit,
                baseline=self.workspace_manager.baseline(snapshot),
            )
        else:
            await self.repository.attach_workspace(
                task_id,
                worker_id=worker_id,
                mode=str(worker["workspace_mode"]),
                path=str(workspace["path"]),
                source_path=str(
                    workspace.get(
                        "source_path", self.workspace_manager.parent_workspace
                    )
                ),
                base_commit=workspace.get("base_commit"),
                baseline=json.loads(str(workspace.get("baseline_json") or "{}")),
            )
        claim = await self.repository.claim_task(
            task_id,
            worker_id,
            reservation=dict(contract.limits),
            account_usage=True,
        )
        self._contracts[task_id] = contract
        self._attempts[task_id] = str(claim["attempt_id"])
        self._workers[task_id] = worker_id

        async def run() -> ValidatedAgentOutput:
            try:
                async with self._semaphore:
                    return await self._run_one(contract)
            finally:
                await self._run_next_for_worker(worker_id)

        running = asyncio.create_task(run())
        self._tasks[task_id] = running
        running.add_done_callback(
            lambda completed: self._background_done(task_id, completed)
        )
        return await self.status(task_id, session_id=session_id)

    async def follow_up_agent(
        self,
        worker_id: str,
        contract: AgentTaskContract,
        *,
        session_id: str,
        plan_task_id: str | None = None,
    ) -> dict[str, Any]:
        worker = await self.repository.worker(worker_id)
        if worker is None:
            raise ValueError("agent worker does not exist")
        team = await self.repository.team(str(worker["team_id"]), session_id=session_id)
        del team
        await self.create_agent_task(
            contract,
            session_id=session_id,
            team_id=str(worker["team_id"]),
            worker_id=worker_id,
            plan_task_id=plan_task_id,
        )
        if str(worker["state"]) in {"idle", "interrupted"}:
            return await self.assign_agent_task(
                contract.task_id, worker_id, session_id=session_id
            )
        return await self.status(contract.task_id, session_id=session_id)

    async def get_team(self, team_id: str, *, session_id: str) -> dict[str, Any]:
        return await self.repository.team(team_id, session_id=session_id)

    async def send_team_message(
        self,
        *,
        payload: dict[str, Any],
        recipient_agent_id: str | None = None,
        team_id: str | None = None,
        broadcast: bool = False,
        session_id: str | None = None,
        source_task_id: str | None = None,
        source_parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        source_agent_id: str | None = None
        if source_task_id is not None:
            source = await self.repository.get_task(source_task_id)
            if source is None or str(source["parent_run_id"]) != source_parent_run_id:
                raise ValueError("source Agent task ownership does not match")
            session_id = str(source["owner_session_id"])
            team_id = str(source["team_id"])
            source_agent_id = source.get("assigned_worker_id")
        if session_id is None:
            raise ValueError("Agent team message requires a session owner")
        if team_id is None and recipient_agent_id is not None:
            recipient = await self.repository.worker(recipient_agent_id)
            if recipient is None:
                raise ValueError("recipient Agent does not exist")
            team_id = str(recipient["team_id"])
        if team_id is None:
            raise ValueError("Agent team message requires a team")
        team = await self.repository.team(team_id, session_id=session_id)
        workers = [
            item
            for item in team["workers"]
            if str(item["state"]) != "stopped"
            and (broadcast or str(item["id"]) == recipient_agent_id)
            and str(item["id"]) != source_agent_id
        ]
        if not broadcast and len(workers) != 1:
            raise ValueError("recipient Agent does not belong to this team")
        delivered: list[dict[str, Any]] = []
        for worker in workers:
            target = await self.repository.one(
                """SELECT id,parent_run_id FROM agent_tasks WHERE assigned_worker_id=?
                   ORDER BY CASE WHEN state IN ('created','ready','claimed','running','waiting_approval')
                                 THEN 0 ELSE 1 END,created_at DESC LIMIT 1""",
                (str(worker["id"]),),
            )
            if target is None:
                continue
            delivered.append(
                await self.repository.send_mailbox(
                    task_id=str(target["id"]),
                    parent_run_id=str(target["parent_run_id"]),
                    sender="system",
                    recipient="child",
                    kind=MailboxMessageKind.INSTRUCTION,
                    payload={
                        "from_agent_id": source_agent_id or "controller",
                        "content_trust": "untrusted_agent",
                        "payload": payload,
                    },
                    ttl_seconds=self.message_ttl_seconds,
                )
            )
        return {
            "team_id": team_id,
            "broadcast": broadcast,
            "delivered": delivered,
            "recipient_count": len(delivered),
        }

    async def stop_agent(self, worker_id: str, *, session_id: str) -> dict[str, Any]:
        worker = await self.repository.worker(worker_id)
        if worker is None:
            raise ValueError("agent worker does not exist")
        await self.repository.team(str(worker["team_id"]), session_id=session_id)
        active = await self.repository.one(
            """SELECT id FROM agent_tasks WHERE assigned_worker_id=?
               AND state IN ('created','ready','claimed','running','waiting_approval')
               ORDER BY created_at LIMIT 1""",
            (worker_id,),
        )
        if active is not None:
            await self.cancel(str(active["id"]))
        workspace = await self.repository.worker_workspace(worker_id)
        if workspace is not None:
            snapshot = WorkspaceSnapshot(
                self.workspace_manager.parent_workspace,
                Path(str(workspace["path"])),
                str(workspace["workspace_mode"]),
            )
            if (
                snapshot.mode != WorkspaceMode.SHARED_READ.value
                and snapshot.root.exists()
            ):
                self.workspace_manager.cleanup(snapshot)
            await self.repository.mark_workspace_cleaned(str(workspace["path"]))
        await self.repository.stop_worker(worker_id)
        value = await self.repository.worker(worker_id)
        assert value is not None
        return value

    async def resume_agent(
        self, worker_id: str, *, session_id: str, task_id: str | None = None
    ) -> dict[str, Any]:
        worker = await self.repository.worker(worker_id)
        if worker is None:
            raise ValueError("agent worker does not exist")
        await self.repository.team(str(worker["team_id"]), session_id=session_id)
        if task_id is None:
            row = await self.repository.one(
                """SELECT id FROM agent_tasks WHERE assigned_worker_id=?
                   AND state='interrupted' ORDER BY created_at DESC LIMIT 1""",
                (worker_id,),
            )
            if row is None:
                raise ValueError("agent has no interrupted task to resume")
            task_id = str(row["id"])
        if not await self.repository.owns_task(task_id, session_id):
            raise ValueError("agent task does not belong to this session")
        if self.child_runner is None or not hasattr(
            self.child_runner, "resume_interrupted"
        ):
            raise RuntimeError("child Agent runner does not support recovery")
        task = await self.repository.get_task(task_id)
        assert task is not None
        contract = AgentTaskContract.from_dict(json.loads(str(task["contract_json"])))
        if contract.digest() != str(task["contract_sha256"]):
            raise ValueError("agent task contract digest does not match")
        self._validate_worker_profile(worker, contract)
        workspace = await self.repository.workspace(task_id)
        if workspace is None or workspace.get("cleaned_at") is not None:
            raise ValueError("interrupted Agent workspace is unavailable")
        prepared = await self.repository.prepare_resume_attempt(
            task_id, worker_id, contract_sha256=contract.digest()
        )
        self._contracts[task_id] = contract
        self._attempts[task_id] = str(prepared["attempt_id"])
        self._workers[task_id] = worker_id

        async def run() -> ValidatedAgentOutput:
            try:
                async with self._semaphore:
                    return await self._run_one(contract, resume=True)
            finally:
                await self._run_next_for_worker(worker_id)

        running = asyncio.create_task(run())
        self._tasks[task_id] = running
        running.add_done_callback(
            lambda completed: self._background_done(task_id, completed)
        )
        return await self.status(task_id, session_id=session_id)

    @staticmethod
    def _validate_worker_profile(
        worker: dict[str, Any], contract: AgentTaskContract
    ) -> None:
        profile = json.loads(str(worker.get("profile_json") or "{}"))
        maximum_capabilities = profile.get("capabilities", [])
        if not isinstance(maximum_capabilities, list):
            raise ValueError("agent profile capabilities must be an array")
        for grant in contract.capabilities:
            if not any(
                isinstance(limit, dict)
                and str(limit.get("kind")) == grant.kind.value
                and limit.get("plugin") == grant.plugin
                and (limit.get("scope") is None or limit.get("scope") == grant.scope)
                for limit in maximum_capabilities
            ):
                raise ValueError(
                    f"task capability exceeds the Agent profile: {grant.kind.value}"
                )
        maximum_paths = profile.get("allowed_paths", [])
        if not isinstance(maximum_paths, list):
            raise ValueError("agent profile allowed_paths must be an array")
        for raw in contract.allowed_paths:
            path = Path(raw)
            if not any(
                path == Path(str(limit)) or Path(str(limit)) in path.parents
                for limit in maximum_paths
            ):
                raise ValueError(f"task path exceeds the Agent profile: {raw}")
        profiles = profile.get("model_profiles")
        if contract.model_profile is not None and (
            not isinstance(profiles, list) or contract.model_profile not in profiles
        ):
            raise ValueError("task model profile exceeds the Agent profile")
        maximum_limits = profile.get("limits", {})
        if not isinstance(maximum_limits, dict):
            raise ValueError("agent profile limits must be an object")
        for name, value in contract.limits.items():
            ceiling = maximum_limits.get(name)
            if (
                ceiling is not None
                and value is not None
                and float(value) > float(ceiling)
            ):
                raise ValueError(f"task limit exceeds the Agent profile: {name}")

    async def cleanup(self, task_id: str) -> None:
        task = await self.repository.get_task(task_id)
        if task is None:
            raise ValueError("child task does not exist")
        if task["state"] in {"created", "running", "waiting_approval"}:
            raise ValueError("active child workspace cannot be cleaned")
        snapshot = await self._snapshot_for(task_id)
        if snapshot.mode != WorkspaceMode.SHARED_READ.value and snapshot.root.exists():
            self.workspace_manager.cleanup(snapshot)
        await self.repository.mark_cleaned(task_id)

    async def audit_approval(
        self,
        contract: AgentTaskContract,
        *,
        decided: bool,
        payload: dict[str, Any],
    ) -> None:
        return await self._audit_log.audit_approval(
            contract, decided=decided, payload=payload
        )

    async def decide_child_approval(
        self,
        parent_action_id: str,
        *,
        session_id: str,
        approve: bool,
    ) -> dict[str, Any]:
        link = await self.repository.approval_for_parent(parent_action_id)
        if link is None or not await self.repository.owns_task(
            str(link["task_id"]), session_id
        ):
            raise ValueError("agent approval does not belong to this session")
        task_id = str(link["task_id"])
        task = await self.repository.get_task(task_id)
        assert task is not None
        contract = AgentTaskContract.from_dict(json.loads(str(task["contract_json"])))
        if contract.digest() != str(link["contract_sha256"]):
            raise ValueError("agent approval contract digest does not match")
        approval_payload = str(link["payload_json"])
        if hashlib.sha256(approval_payload.encode("utf-8")).hexdigest() != str(
            link["action_sha256"]
        ):
            raise ValueError("agent approval action digest does not match")
        resume = getattr(self.child_runner, "resume_approval", None)
        if resume is None:
            raise ValueError("child Agent continuation is unavailable")
        await self.repository.decide_approval_link(
            parent_action_id, "approved" if approve else "rejected"
        )
        attempt_id = str(link["attempt_id"])
        await self.repository.resume_attempt(attempt_id)
        self._attempts[task_id] = attempt_id

        async def continue_child() -> ValidatedAgentOutput:
            async with self._semaphore:
                try:
                    raw = await resume(
                        task_id,
                        child_action_id=str(link["child_action_id"]),
                        approve=approve,
                    )
                    return await self._complete_resumed(contract, raw, attempt_id)
                except ChildApprovalPending as exc:
                    await self.repository.finish_attempt(
                        attempt_id, "suspended", error=str(exc)
                    )
                    return ValidatedAgentOutput(
                        task_id=task_id,
                        state=AgentTaskState.WAITING_APPROVAL,
                        summary="",
                        error=str(exc),
                    )

        running = asyncio.create_task(continue_child())
        self._tasks[task_id] = running
        self._contracts[task_id] = contract
        running.add_done_callback(
            lambda completed: self._background_done(task_id, completed)
        )
        return await self.status(task_id, session_id=session_id)

    async def _complete_resumed(
        self,
        contract: AgentTaskContract,
        raw: dict[str, Any],
        attempt_id: str,
    ) -> ValidatedAgentOutput:
        snapshot = await self._snapshot_for(contract.task_id)
        try:
            child_run_id = raw.get("_child_run_id")
            if child_run_id:
                await self.repository.set_state(
                    contract.task_id,
                    AgentTaskState.RUNNING,
                    child_run_id=str(child_run_id),
                )
            await self._audit(
                contract, AgentMessageKind.MESSAGE_RECEIVED, {"output": raw}
            )
            verified = self.verifier.verify(contract, snapshot, raw)
            if snapshot.mode == WorkspaceMode.SHARED_READ.value:
                if verified.artifacts:
                    raise VerificationError(
                        "shared_read Agent output cannot publish artifacts"
                    )
            else:
                self.workspace_manager.publish_artifacts(
                    snapshot,
                    verified.artifacts,
                    allowed_paths=contract.allowed_paths,
                )
                await self.repository.update_workspace_baseline(
                    str(snapshot.root), self.workspace_manager.baseline(snapshot)
                )
        except Exception as exc:
            rejected = self.verifier.rejected(contract, str(exc))
            await self.repository.set_state(
                contract.task_id, AgentTaskState.FAILED, error=str(exc)
            )
            await self.repository.record_output(rejected)
            await self.repository.finish_attempt(attempt_id, "failed", error=str(exc))
            await self.repository.mark_retained(contract.task_id)
            return rejected
        await self.repository.set_state(contract.task_id, AgentTaskState.COMPLETED)
        await self.repository.record_output(verified)
        await self.repository.finish_attempt(
            attempt_id, "completed", usage=dict(verified.usage)
        )
        await self._audit(
            contract,
            AgentMessageKind.TASK_FINISHED,
            {"state": AgentTaskState.COMPLETED.value},
        )
        return verified

    async def _run_one(
        self, contract: AgentTaskContract, *, resume: bool = False
    ) -> ValidatedAgentOutput:
        snapshot = await self._snapshot_for(contract.task_id)
        attempt_id = self._attempts.get(contract.task_id)
        if attempt_id is None:
            latest_attempt = await self.repository.latest_attempt(contract.task_id)
            if latest_attempt is not None and str(latest_attempt["state"]) in {
                "created",
                "running",
                "suspended",
            }:
                attempt_id = str(latest_attempt["id"])
        if attempt_id is not None:
            await self.repository.start_attempt(attempt_id)
        else:
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
            if resume:
                resume_interrupted = getattr(
                    self.child_runner, "resume_interrupted", None
                )
                if resume_interrupted is None:
                    raise RuntimeError("child Agent runner does not support recovery")
                raw = await resume_interrupted(contract, snapshot)
            else:
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
            if attempt_id is not None:
                await self.repository.finish_attempt(
                    attempt_id, "suspended", error=str(exc)
                )
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
            if attempt_id is not None:
                await self.repository.finish_attempt(
                    attempt_id, "failed", usage=dict(verified.usage), error=str(exc)
                )
            return verified
        try:
            if snapshot.mode == WorkspaceMode.SHARED_READ.value:
                if verified.artifacts:
                    raise VerificationError(
                        "shared_read Agent output cannot publish artifacts"
                    )
            else:
                self.workspace_manager.publish_artifacts(
                    snapshot,
                    verified.artifacts,
                    allowed_paths=contract.allowed_paths,
                )
                await self.repository.update_workspace_baseline(
                    str(snapshot.root), self.workspace_manager.baseline(snapshot)
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
            if attempt_id is not None:
                await self.repository.finish_attempt(
                    attempt_id, "failed", usage=dict(rejected.usage), error=str(exc)
                )
            return rejected
        await self.repository.set_state(contract.task_id, AgentTaskState.COMPLETED)
        await self.repository.record_output(verified)
        if attempt_id is not None:
            await self.repository.finish_attempt(
                attempt_id, "completed", usage=dict(verified.usage)
            )
        if verified.memory_proposals and self.proposal_handler is not None:
            try:
                await self.proposal_handler(contract, verified)
            except Exception:
                # Promotion is an optional parent-side persistence path and must not
                # invalidate an otherwise verified child result.
                pass
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
        worker_id = self._workers.get(contract.task_id)
        worker = (
            await self.repository.worker(worker_id) if worker_id is not None else None
        )
        if snapshot.root.exists() and not (worker and bool(worker.get("persistent"))):
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
            AgentTaskState.READY.value,
            AgentTaskState.CLAIMED.value,
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
        attempt_id = self._attempts.get(contract.task_id)
        if attempt_id is None:
            latest_attempt = await self.repository.latest_attempt(contract.task_id)
            if latest_attempt is not None and str(latest_attempt["state"]) in {
                "created",
                "running",
                "suspended",
            }:
                attempt_id = str(latest_attempt["id"])
        if attempt_id is not None:
            await self.repository.finish_attempt(attempt_id, "cancelled", error=reason)
        await self._audit(contract, AgentMessageKind.TASK_CANCELLED, {"reason": reason})
        await self._audit(
            contract,
            AgentMessageKind.TASK_FINISHED,
            {"state": AgentTaskState.CANCELLED.value},
        )
        await self.repository.mark_retained(contract.task_id)
        return output

    async def _snapshot_for(self, task_id: str) -> WorkspaceSnapshot:
        return await self._artifact_publisher._snapshot_for(task_id)

    async def _audit(
        self,
        contract: AgentTaskContract,
        kind: AgentMessageKind,
        payload: dict[str, Any],
    ) -> None:
        return await self._audit_log._audit(contract, kind, payload)

    async def _audit_by_id(
        self,
        task_id: str,
        kind: AgentMessageKind,
        payload: dict[str, Any],
        *,
        parent_run_id: str | None = None,
    ) -> None:
        return await self._audit_log._audit_by_id(
            task_id, kind, payload, parent_run_id=parent_run_id
        )
