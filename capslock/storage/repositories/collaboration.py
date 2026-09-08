"""Persistence for parent/child collaboration contracts and audit records."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ...collaboration.models import (
    AgentMessage,
    AgentTaskContract,
    AgentTaskState,
    ValidatedAgentOutput,
    MailboxMessageKind,
)
from ...security import redact
from .core import Repository, now


class CollaborationRepository(Repository):
    async def interrupt_active(self) -> int:
        timestamp = now()
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE agent_tasks SET state='interrupted',error='interrupted during process restart',
                   finished_at=? WHERE state IN ('created','ready','claimed','running','waiting_approval')""",
                (timestamp,),
            )
            await connection.execute(
                """UPDATE agent_attempts SET state='interrupted',error='interrupted during process restart',
                   finished_at=? WHERE state IN ('created','running','suspended')""",
                (timestamp,),
            )
            await connection.execute(
                """UPDATE agent_workers SET state='interrupted',updated_at=?
                   WHERE state IN ('starting','running','waiting_approval')""",
                (timestamp,),
            )
            return int(cursor.rowcount)

    async def ensure_default_team(
        self, session_id: str, *, created_by_run_id: str | None = None
    ) -> str:
        identifier = f"default:{session_id}"
        await self.execute(
            """INSERT OR IGNORE INTO agent_teams(
                   id,session_id,name,state,created_by_run_id,created_at)
               VALUES(?,?,'default','active',?,?)""",
            (identifier, session_id, created_by_run_id, now()),
        )
        return identifier

    async def create_team(
        self, session_id: str, name: str, *, created_by_run_id: str | None = None
    ) -> dict[str, Any]:
        normalized = name.strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("agent team name must be between 1 and 64 characters")
        identifier = f"team_{uuid.uuid4().hex}"
        await self.execute(
            """INSERT INTO agent_teams(id,session_id,name,state,created_by_run_id,created_at)
               VALUES(?,?,?,'active',?,?)""",
            (identifier, session_id, normalized, created_by_run_id, now()),
        )
        return dict(
            await self.one("SELECT * FROM agent_teams WHERE id=?", (identifier,))
        )

    async def create_worker(
        self,
        team_id: str,
        name: str,
        *,
        profile: dict[str, Any] | None = None,
        workspace_mode: str = "snapshot",
        baseline: dict[str, str] | None = None,
        persistent: bool = True,
    ) -> dict[str, Any]:
        if workspace_mode not in {"snapshot", "worktree", "shared_read"}:
            raise ValueError("invalid agent workspace mode")
        normalized = name.strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("agent name must be between 1 and 64 characters")
        if (
            await self.one(
                "SELECT id FROM agent_teams WHERE id=? AND state='active'", (team_id,)
            )
            is None
        ):
            raise ValueError("active agent team does not exist")
        identifier = f"agent_{uuid.uuid4().hex}"
        timestamp = now()
        await self.execute(
            """INSERT INTO agent_workers(
                   id,team_id,name,profile_json,workspace_mode,state,persistent,created_at,updated_at)
               VALUES(?,?,?,?,?,'idle',?,?,?)""",
            (
                identifier,
                team_id,
                normalized,
                json.dumps(redact(profile or {}), ensure_ascii=False, sort_keys=True),
                workspace_mode,
                int(persistent),
                timestamp,
                timestamp,
            ),
        )
        return dict(
            await self.one("SELECT * FROM agent_workers WHERE id=?", (identifier,))
        )

    async def create_task(
        self,
        contract: AgentTaskContract,
        *,
        workspace_path: str | None = None,
        source_path: str = "",
        baseline: dict[str, str] | None = None,
        team_id: str | None = None,
        worker_id: str | None = None,
        plan_task_id: str | None = None,
        priority: int = 0,
        depends_on: tuple[str, ...] = (),
        workspace_mode: str = "snapshot",
    ) -> None:
        timestamp = now()
        owner = await self.one(
            "SELECT session_id FROM runs WHERE id=?", (contract.parent_run_id,)
        )
        if owner is None:
            raise ValueError("parent run does not exist")
        session_id = str(owner["session_id"])
        selected_team = team_id or await self.ensure_default_team(
            session_id, created_by_run_id=contract.parent_run_id
        )
        team = await self.one(
            "SELECT session_id FROM agent_teams WHERE id=? AND state='active'",
            (selected_team,),
        )
        if team is None or str(team["session_id"]) != session_id:
            raise ValueError("agent team does not belong to the parent session")
        if worker_id is not None:
            worker = await self.one(
                "SELECT team_id FROM agent_workers WHERE id=? AND state<>'stopped'",
                (worker_id,),
            )
            if worker is None or str(worker["team_id"]) != selected_team:
                raise ValueError("agent worker does not belong to the task team")
        if plan_task_id is not None:
            plan_task = await self.one(
                "SELECT session_id FROM tasks WHERE id=?", (plan_task_id,)
            )
            if plan_task is None or str(plan_task["session_id"]) != session_id:
                raise ValueError("linked plan task must belong to the parent session")
        for dependency in depends_on:
            row = await self.get_task(dependency)
            if row is None or str(row.get("team_id")) != selected_team:
                raise ValueError("agent task dependency must belong to the same team")
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO agent_tasks(
                       id,parent_run_id,owner_session_id,team_id,assigned_worker_id,plan_task_id,
                       objective,contract_json,contract_sha256,priority,state,child_workspace,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                (
                    contract.task_id,
                    contract.parent_run_id,
                    session_id,
                    selected_team,
                    worker_id,
                    plan_task_id,
                    contract.objective,
                    json.dumps(contract.as_dict(), ensure_ascii=False),
                    contract.digest(),
                    int(priority),
                    AgentTaskState.CREATED.value,
                    timestamp,
                ),
            )
            if workspace_path is not None:
                await connection.execute(
                    """INSERT INTO agent_workspaces(
                           task_id,worker_id,workspace_mode,path,source_path,baseline_json,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        contract.task_id,
                        worker_id,
                        workspace_mode,
                        workspace_path,
                        source_path,
                        json.dumps(baseline or {}, sort_keys=True),
                        timestamp,
                    ),
                )
            for dependency in depends_on:
                await connection.execute(
                    """INSERT INTO agent_task_dependencies(
                           task_id,blocked_by_task_id,created_at) VALUES(?,?,?)""",
                    (contract.task_id, dependency, timestamp),
                )
        if depends_on and await self._dependency_cycle(contract.task_id):
            await self.execute(
                "DELETE FROM agent_tasks WHERE id=?", (contract.task_id,)
            )
            raise ValueError("agent task dependencies would create a cycle")

    async def set_state(
        self,
        task_id: str,
        state: AgentTaskState,
        *,
        child_run_id: str | None = None,
        error: str | None = None,
    ) -> None:
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError("child task does not exist")
        current = AgentTaskState(str(task["state"]))
        allowed = {
            AgentTaskState.CREATED: {
                AgentTaskState.BLOCKED,
                AgentTaskState.READY,
                AgentTaskState.CLAIMED,
                AgentTaskState.RUNNING,
                AgentTaskState.FAILED,
                AgentTaskState.CANCELLED,
                AgentTaskState.INTERRUPTED,
            },
            AgentTaskState.BLOCKED: {
                AgentTaskState.READY,
                AgentTaskState.CANCELLED,
                AgentTaskState.INTERRUPTED,
            },
            AgentTaskState.READY: {
                AgentTaskState.CLAIMED,
                AgentTaskState.CANCELLED,
                AgentTaskState.INTERRUPTED,
            },
            AgentTaskState.CLAIMED: {
                AgentTaskState.RUNNING,
                AgentTaskState.CANCELLED,
                AgentTaskState.INTERRUPTED,
            },
            AgentTaskState.RUNNING: {
                AgentTaskState.WAITING_APPROVAL,
                AgentTaskState.COMPLETED,
                AgentTaskState.FAILED,
                AgentTaskState.CANCELLED,
                AgentTaskState.INTERRUPTED,
            },
            AgentTaskState.WAITING_APPROVAL: {
                AgentTaskState.RUNNING,
                AgentTaskState.COMPLETED,
                AgentTaskState.FAILED,
                AgentTaskState.CANCELLED,
                AgentTaskState.INTERRUPTED,
            },
        }
        if state is not current and state not in allowed.get(current, set()):
            raise ValueError(
                f"invalid child task transition: {current.value} -> {state.value}"
            )
        timestamp = now()
        started = timestamp if state is AgentTaskState.RUNNING else None
        finished = (
            timestamp
            if state
            in {
                AgentTaskState.COMPLETED,
                AgentTaskState.FAILED,
                AgentTaskState.CANCELLED,
                AgentTaskState.INTERRUPTED,
            }
            else None
        )
        updated = await self.execute(
            """UPDATE agent_tasks SET state=?,child_run_id=coalesce(?,child_run_id),error=?,
               started_at=coalesce(started_at,?),finished_at=coalesce(?,finished_at)
               WHERE id=? AND state=?""",
            (
                state.value,
                child_run_id,
                error,
                started,
                finished,
                task_id,
                current.value,
            ),
        )
        if not updated:
            raise ValueError("child task changed concurrently")

    async def record_message(self, message: AgentMessage) -> None:
        payload = message.safe_payload
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT coalesce(max(sequence),0)+1 FROM agent_messages WHERE task_id=?",
                    (message.task_id,),
                )
            ).fetchone()
            if row is None or int(row[0]) != message.sequence:
                raise ValueError("agent message sequence is invalid or replayed")
            await connection.execute(
                """INSERT INTO agent_messages(id,task_id,parent_run_id,sender,recipient,sequence,message_kind,
                   payload_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    message.message_id,
                    message.task_id,
                    message.parent_run_id,
                    message.sender,
                    message.recipient,
                    message.sequence,
                    message.kind.value,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    message.payload_digest,
                    message.created_at,
                ),
            )

    async def record_output(self, output: ValidatedAgentOutput) -> None:
        encoded = json.dumps(output.as_dict(), ensure_ascii=False, sort_keys=True)
        attempt = await self.one(
            "SELECT id FROM agent_attempts WHERE task_id=? ORDER BY ordinal DESC LIMIT 1",
            (output.task_id,),
        )
        await self.execute(
            """INSERT INTO agent_outputs(
                   task_id,attempt_id,state,output_json,verified,output_sha256,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                output.task_id,
                None if attempt is None else str(attempt["id"]),
                output.state.value,
                encoded,
                int(output.verified),
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                now(),
            ),
        )

    async def send_mailbox(
        self,
        *,
        task_id: str,
        parent_run_id: str,
        sender: str,
        recipient: str,
        kind: MailboxMessageKind,
        payload: dict[str, Any],
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        if (sender, recipient) not in {
            ("parent", "child"),
            ("child", "parent"),
            ("system", "parent"),
            ("system", "child"),
        }:
            raise ValueError("invalid agent mailbox route")
        if not isinstance(kind, MailboxMessageKind):
            raise ValueError("invalid agent mailbox message kind")
        safe = redact(payload)
        encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded.encode("utf-8")) > 32_000:
            raise ValueError("agent mailbox message exceeds 32 KiB")
        identifier = f"mail_{uuid.uuid4().hex}"
        created = datetime.now(UTC)
        expires = created + timedelta(seconds=min(max(ttl_seconds, 1), 86_400))
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError("child task does not exist")
        attempt = await self.one(
            "SELECT id FROM agent_attempts WHERE task_id=? ORDER BY ordinal DESC LIMIT 1",
            (task_id,),
        )
        await self.execute(
            """INSERT INTO agent_mailbox(id,task_id,parent_run_id,team_id,worker_id,attempt_id,
               sender,recipient,message_kind,payload_json,payload_sha256,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                task_id,
                parent_run_id,
                task.get("team_id"),
                task.get("assigned_worker_id"),
                None if attempt is None else str(attempt["id"]),
                sender,
                recipient,
                kind.value,
                encoded,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                created.isoformat(),
                expires.isoformat(),
            ),
        )
        return (await self.mailbox_message(identifier)) or {}

    async def mailbox_message(self, identifier: str) -> dict[str, Any] | None:
        row = await self.one("SELECT * FROM agent_mailbox WHERE id=?", (identifier,))
        return None if row is None else _mailbox(dict(row))

    async def receive_mailbox(
        self,
        task_id: str,
        *,
        recipient: str,
        mark_delivered: bool = True,
    ) -> list[dict[str, Any]]:
        timestamp = now()
        await self.execute(
            """UPDATE agent_mailbox SET status='expired' WHERE task_id=? AND recipient=?
               AND status IN ('queued','delivered') AND expires_at IS NOT NULL AND expires_at<=?""",
            (task_id, recipient, timestamp),
        )
        if mark_delivered:
            await self.execute(
                """UPDATE agent_mailbox SET status='delivered',delivered_at=coalesce(delivered_at,?)
                   WHERE task_id=? AND recipient=? AND status='queued'""",
                (timestamp, task_id, recipient),
            )
        rows = await self.all(
            """SELECT * FROM agent_mailbox WHERE task_id=? AND recipient=?
               AND status IN ('queued','delivered') ORDER BY created_at,id""",
            (task_id, recipient),
        )
        return [_mailbox(dict(row)) for row in rows]

    async def receive_worker_mailbox(
        self, worker_id: str, *, mark_delivered: bool = True
    ) -> list[dict[str, Any]]:
        timestamp = now()
        await self.execute(
            """UPDATE agent_mailbox SET status='expired' WHERE worker_id=? AND recipient='child'
               AND status IN ('queued','delivered') AND expires_at IS NOT NULL AND expires_at<=?""",
            (worker_id, timestamp),
        )
        if mark_delivered:
            await self.execute(
                """UPDATE agent_mailbox SET status='delivered',delivered_at=coalesce(delivered_at,?)
                   WHERE worker_id=? AND recipient='child' AND status='queued'""",
                (timestamp, worker_id),
            )
        rows = await self.all(
            """SELECT * FROM agent_mailbox WHERE worker_id=? AND recipient='child'
               AND status IN ('queued','delivered') ORDER BY created_at,id""",
            (worker_id,),
        )
        return [_mailbox(dict(row)) for row in rows]

    async def acknowledge_mailbox(self, identifier: str, *, recipient: str) -> None:
        updated = await self.execute(
            """UPDATE agent_mailbox SET status='acknowledged',acknowledged_at=?
               WHERE id=? AND recipient=? AND status='delivered'""",
            (now(), identifier, recipient),
        )
        if not updated:
            raise ValueError("mailbox message is not delivered to this recipient")

    async def get_output(self, task_id: str) -> ValidatedAgentOutput | None:
        row = await self.one(
            "SELECT output_json,output_sha256 FROM agent_outputs WHERE task_id=?",
            (task_id,),
        )
        if row is None:
            return None
        encoded = str(row["output_json"])
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(
            row["output_sha256"]
        ):
            raise ValueError("stored child output digest does not match")
        value = json.loads(encoded)
        if str(value.get("task_id", "")) != task_id:
            raise ValueError("stored child output task id does not match")
        return ValidatedAgentOutput(
            task_id=str(value["task_id"]),
            state=AgentTaskState(value["state"]),
            summary=str(value.get("summary", "")),
            evidence=tuple(value.get("evidence", ())),
            artifacts=tuple(value.get("artifacts", ())),
            checks=tuple(value.get("checks", ())),
            usage=value.get("usage", {}),
            verified=bool(value.get("verified", False)),
            content_trust=str(value.get("content_trust", "untrusted_agent")),
            verification_scope=dict(value.get("verification_scope", {})),
            error=value.get("error"),
            memory_proposals=tuple(value.get("memory_proposals", ())),
        )

    async def list_tasks(self, parent_run_id: str) -> list[dict[str, Any]]:
        rows = await self.all(
            "SELECT * FROM agent_tasks WHERE parent_run_id=? ORDER BY created_at,id",
            (parent_run_id,),
        )
        return [dict(row) for row in rows]

    async def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.all(
            """SELECT t.* FROM agent_tasks t
               WHERE t.owner_session_id=? ORDER BY t.created_at,t.id""",
            (session_id,),
        )
        return [dict(row) for row in rows]

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = await self.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        return None if row is None else dict(row)

    async def workspace(self, task_id: str) -> dict[str, Any] | None:
        row = await self.one(
            "SELECT path,retained,cleaned_at FROM agent_workspaces WHERE task_id=?",
            (task_id,),
        )
        return None if row is None else dict(row)

    async def attach_workspace(
        self,
        task_id: str,
        *,
        worker_id: str | None,
        mode: str,
        path: str,
        source_path: str,
        base_commit: str | None = None,
        baseline: dict[str, str] | None = None,
    ) -> None:
        await self.execute(
            """INSERT INTO agent_workspaces(
                   task_id,worker_id,workspace_mode,path,source_path,base_commit,baseline_json,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                task_id,
                worker_id,
                mode,
                path,
                source_path,
                base_commit,
                json.dumps(baseline or {}, sort_keys=True),
                now(),
            ),
        )

    async def update_workspace_baseline(
        self, path: str, baseline: dict[str, str]
    ) -> None:
        updated = await self.execute(
            """UPDATE agent_workspaces SET baseline_json=?
               WHERE path=? AND cleaned_at IS NULL""",
            (json.dumps(baseline, sort_keys=True), path),
        )
        if not updated:
            raise ValueError("active agent workspace does not exist")

    async def worker(self, worker_id: str) -> dict[str, Any] | None:
        row = await self.one("SELECT * FROM agent_workers WHERE id=?", (worker_id,))
        return None if row is None else dict(row)

    async def worker_workspace(self, worker_id: str) -> dict[str, Any] | None:
        row = await self.one(
            """SELECT w.* FROM agent_workspaces w WHERE w.worker_id=?
               AND w.cleaned_at IS NULL ORDER BY w.created_at LIMIT 1""",
            (worker_id,),
        )
        return None if row is None else dict(row)

    async def stop_worker(self, worker_id: str) -> None:
        updated = await self.execute(
            """UPDATE agent_workers SET state='stopped',updated_at=?,stopped_at=?
               WHERE id=? AND state<>'stopped'""",
            (now(), now(), worker_id),
        )
        if not updated:
            raise ValueError("active agent worker does not exist")

    async def latest_attempt(self, task_id: str) -> dict[str, Any] | None:
        row = await self.one(
            "SELECT * FROM agent_attempts WHERE task_id=? ORDER BY ordinal DESC LIMIT 1",
            (task_id,),
        )
        return None if row is None else dict(row)

    async def checkpoint_attempt(
        self,
        attempt_id: str,
        *,
        contract_sha256: str,
        child_session_id: str | None,
        child_run_id: str | None,
        checkpoint: dict[str, Any],
        resumable: bool,
    ) -> None:
        timestamp = now()
        async with self.database.transaction() as connection:
            attempt = await (
                await connection.execute(
                    "SELECT worker_id FROM agent_attempts WHERE id=?", (attempt_id,)
                )
            ).fetchone()
            if attempt is None:
                raise ValueError("agent attempt does not exist")
            await connection.execute(
                """INSERT INTO agent_checkpoints(
                       attempt_id,contract_sha256,child_session_id,child_run_id,
                       checkpoint_json,resumable,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(attempt_id) DO UPDATE SET
                     contract_sha256=excluded.contract_sha256,
                     child_session_id=excluded.child_session_id,
                     child_run_id=excluded.child_run_id,
                     checkpoint_json=excluded.checkpoint_json,
                     resumable=excluded.resumable,
                     updated_at=excluded.updated_at""",
                (
                    attempt_id,
                    contract_sha256,
                    child_session_id,
                    child_run_id,
                    json.dumps(redact(checkpoint), ensure_ascii=False, sort_keys=True),
                    int(resumable),
                    timestamp,
                ),
            )
            if attempt["worker_id"] is not None and child_session_id is not None:
                await connection.execute(
                    """UPDATE agent_workers SET child_session_id=?,updated_at=?
                       WHERE id=?""",
                    (child_session_id, timestamp, str(attempt["worker_id"])),
                )

    async def link_approval(
        self,
        *,
        task_id: str,
        child_action_id: str,
        parent_action_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        task = await self.get_task(task_id)
        attempt = await self.latest_attempt(task_id)
        if task is None or attempt is None:
            raise ValueError("agent attempt does not exist for approval")
        safe = redact(payload)
        encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
        action_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        identifier = f"approval_{uuid.uuid4().hex}"
        await self.execute(
            """INSERT INTO agent_approval_links(
                   id,attempt_id,child_action_id,parent_action_id,action_sha256,
                   contract_sha256,state,payload_json,created_at)
               VALUES(?,?,?,?,?,?,'pending',?,?)""",
            (
                identifier,
                str(attempt["id"]),
                child_action_id,
                parent_action_id,
                action_digest,
                str(task["contract_sha256"]),
                encoded,
                now(),
            ),
        )
        row = await self.one(
            "SELECT * FROM agent_approval_links WHERE id=?", (identifier,)
        )
        assert row is not None
        return dict(row)

    async def approval_for_parent(self, parent_action_id: str) -> dict[str, Any] | None:
        row = await self.one(
            """SELECT l.*,a.task_id FROM agent_approval_links l
               JOIN agent_attempts a ON a.id=l.attempt_id
               WHERE l.parent_action_id=?""",
            (parent_action_id,),
        )
        return None if row is None else dict(row)

    async def decide_approval_link(
        self, parent_action_id: str, decision: str
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("invalid agent approval decision")
        updated = await self.execute(
            """UPDATE agent_approval_links SET state=?,decided_at=?
               WHERE parent_action_id=? AND state='pending'""",
            (decision, now(), parent_action_id),
        )
        if not updated:
            raise ValueError("agent approval is not pending")
        row = await self.approval_for_parent(parent_action_id)
        assert row is not None
        return row

    async def messages(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self.all(
            "SELECT * FROM agent_messages WHERE task_id=? ORDER BY sequence", (task_id,)
        )
        messages = [dict(row) for row in rows]
        for expected, message in enumerate(messages, start=1):
            if int(message["sequence"]) != expected:
                raise ValueError("stored agent message sequence is invalid")
            payload = json.loads(str(message["payload_json"]))
            digest = (
                str(payload.get("sha256"))
                if isinstance(payload, dict) and payload.get("truncated") is True
                else hashlib.sha256(
                    json.dumps(
                        payload,
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
            )
            if digest != str(message["payload_sha256"]):
                raise ValueError("stored agent message digest does not match")
        return messages

    async def active_tasks(self, parent_run_id: str) -> list[str]:
        rows = await self.all(
            "SELECT id FROM agent_tasks WHERE parent_run_id=? AND state IN ('created','running','waiting_approval') ORDER BY created_at,id",
            (parent_run_id,),
        )
        return [str(row[0]) for row in rows]

    async def active_for_session(self, session_id: str) -> list[str]:
        rows = await self.all(
            """SELECT id FROM agent_tasks WHERE owner_session_id=?
               AND state IN ('created','ready','claimed','running','waiting_approval')
               ORDER BY created_at,id""",
            (session_id,),
        )
        return [str(row[0]) for row in rows]

    async def owns_task(self, task_id: str, session_id: str) -> bool:
        return (
            await self.one(
                "SELECT 1 FROM agent_tasks WHERE id=? AND owner_session_id=?",
                (task_id, session_id),
            )
            is not None
        )

    async def team(self, team_id: str, *, session_id: str) -> dict[str, Any]:
        team = await self.one(
            "SELECT * FROM agent_teams WHERE id=? AND session_id=?",
            (team_id, session_id),
        )
        if team is None:
            raise ValueError("agent team does not belong to this session")
        workers = await self.all(
            "SELECT * FROM agent_workers WHERE team_id=? ORDER BY created_at,id",
            (team_id,),
        )
        tasks = await self.all(
            "SELECT * FROM agent_tasks WHERE team_id=? ORDER BY priority DESC,created_at,id",
            (team_id,),
        )
        attempts = await self.all(
            """SELECT a.* FROM agent_attempts a JOIN agent_tasks t ON t.id=a.task_id
               WHERE t.team_id=? ORDER BY a.created_at,a.id""",
            (team_id,),
        )
        approvals = await self.all(
            """SELECT l.* FROM agent_approval_links l JOIN agent_attempts a ON a.id=l.attempt_id
               JOIN agent_tasks t ON t.id=a.task_id WHERE t.team_id=?
               ORDER BY l.created_at,l.id""",
            (team_id,),
        )
        budget = await self.all(
            "SELECT * FROM agent_budget_ledger WHERE team_id=? ORDER BY created_at,id",
            (team_id,),
        )
        return {
            "team": dict(team),
            "workers": [dict(row) for row in workers],
            "tasks": [dict(row) for row in tasks],
            "attempts": [dict(row) for row in attempts],
            "approvals": [dict(row) for row in approvals],
            "budget": [dict(row) for row in budget],
        }

    async def claim_task(
        self,
        task_id: str,
        worker_id: str,
        *,
        reservation: dict[str, Any] | None = None,
        account_usage: bool = False,
    ) -> dict[str, Any]:
        token = f"claim_{uuid.uuid4().hex}"
        attempt_id = f"attempt_{uuid.uuid4().hex}"
        timestamp = now()
        async with self.database.transaction() as connection:
            task = await (
                await connection.execute(
                    "SELECT * FROM agent_tasks WHERE id=?", (task_id,)
                )
            ).fetchone()
            if task is None:
                raise ValueError("agent task does not exist")
            worker = await (
                await connection.execute(
                    "SELECT * FROM agent_workers WHERE id=? AND state<>'stopped'",
                    (worker_id,),
                )
            ).fetchone()
            if worker is None or str(worker["team_id"]) != str(task["team_id"]):
                raise ValueError("agent worker does not belong to the task team")
            if str(task["state"]) not in {"created", "ready", "interrupted"}:
                raise ValueError("agent task is not claimable")
            incomplete = await (
                await connection.execute(
                    """SELECT count(*) FROM agent_task_dependencies d
                       JOIN agent_tasks p ON p.id=d.blocked_by_task_id
                       WHERE d.task_id=? AND p.state<>'completed'""",
                    (task_id,),
                )
            ).fetchone()
            if incomplete is not None and int(incomplete[0]) > 0:
                raise ValueError("agent task dependencies are not completed")
            ordinal = int(task["attempt_count"]) + 1
            updated = await connection.execute(
                """UPDATE agent_tasks SET state='claimed',assigned_worker_id=?,claim_token=?,
                   attempt_count=? WHERE id=? AND state=?""",
                (worker_id, token, ordinal, task_id, str(task["state"])),
            )
            if updated.rowcount != 1:
                raise ValueError("agent task changed concurrently")
            await connection.execute(
                """INSERT INTO agent_attempts(
                       id,task_id,worker_id,ordinal,state,claim_token,reservation_json,created_at)
                   VALUES(?,?,?,?,'created',?,?,?)""",
                (
                    attempt_id,
                    task_id,
                    worker_id,
                    ordinal,
                    token,
                    json.dumps(
                        {
                            **(reservation or {}),
                            "_account_usage_in_parent_session": account_usage,
                        },
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
            await connection.execute(
                "UPDATE agent_workers SET state='starting',updated_at=? WHERE id=?",
                (timestamp, worker_id),
            )
            if reservation:
                await connection.execute(
                    """INSERT INTO agent_budget_ledger(
                           id,team_id,task_id,attempt_id,operation,amount_json,idempotency_key,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        f"budget_{uuid.uuid4().hex}",
                        str(task["team_id"]),
                        task_id,
                        attempt_id,
                        "reserve",
                        json.dumps(reservation, sort_keys=True),
                        f"reserve:{attempt_id}",
                        timestamp,
                    ),
                )
        return {"attempt_id": attempt_id, "claim_token": token}

    async def start_attempt(
        self, attempt_id: str, *, child_run_id: str | None = None
    ) -> None:
        timestamp = now()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT task_id,worker_id,state FROM agent_attempts WHERE id=?",
                    (attempt_id,),
                )
            ).fetchone()
            if row is None or str(row["state"]) != "created":
                raise ValueError("agent attempt is not startable")
            await connection.execute(
                """UPDATE agent_attempts SET state='running',child_run_id=?,started_at=?
                   WHERE id=? AND state='created'""",
                (child_run_id, timestamp, attempt_id),
            )
            await connection.execute(
                "UPDATE agent_tasks SET state='running',child_run_id=coalesce(?,child_run_id) WHERE id=?",
                (child_run_id, str(row["task_id"])),
            )
            if row["worker_id"] is not None:
                await connection.execute(
                    "UPDATE agent_workers SET state='running',updated_at=? WHERE id=?",
                    (timestamp, str(row["worker_id"])),
                )

    async def finish_attempt(
        self,
        attempt_id: str,
        state: str,
        *,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if state not in {
            "suspended",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise ValueError("invalid terminal agent attempt state")
        timestamp = now()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    """SELECT a.task_id,a.worker_id,a.reservation_json,t.team_id,
                              t.parent_run_id,a.state,w.persistent
                       FROM agent_attempts a JOIN agent_tasks t ON t.id=a.task_id
                       LEFT JOIN agent_workers w ON w.id=a.worker_id WHERE a.id=?""",
                    (attempt_id,),
                )
            ).fetchone()
            if row is None:
                raise ValueError("agent attempt does not exist")
            if str(row["state"]) in {"completed", "failed", "cancelled", "interrupted"}:
                return
            await connection.execute(
                """UPDATE agent_attempts SET state=?,usage_json=?,error=?,finished_at=? WHERE id=?""",
                (
                    state,
                    json.dumps(usage or {}, sort_keys=True),
                    error,
                    timestamp,
                    attempt_id,
                ),
            )
            task_state = "waiting_approval" if state == "suspended" else state
            await connection.execute(
                "UPDATE agent_tasks SET state=?,error=?,finished_at=? WHERE id=?",
                (
                    task_state,
                    error,
                    None if state == "suspended" else timestamp,
                    str(row["task_id"]),
                ),
            )
            if row["worker_id"] is not None:
                worker_state = (
                    "waiting_approval"
                    if state == "suspended"
                    else ("idle" if bool(row["persistent"]) else "stopped")
                )
                await connection.execute(
                    """UPDATE agent_workers SET state=?,updated_at=?,
                       stopped_at=CASE WHEN ?='stopped' THEN ? ELSE stopped_at END WHERE id=?""",
                    (
                        worker_state,
                        timestamp,
                        worker_state,
                        timestamp,
                        str(row["worker_id"]),
                    ),
                )
            if state != "suspended":
                await connection.execute(
                    """INSERT OR IGNORE INTO agent_budget_ledger(
                           id,team_id,task_id,attempt_id,operation,amount_json,idempotency_key,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        f"budget_{uuid.uuid4().hex}",
                        str(row["team_id"]),
                        str(row["task_id"]),
                        attempt_id,
                        "settle" if usage else "release",
                        json.dumps(usage or {}, sort_keys=True),
                        f"finish:{attempt_id}",
                        timestamp,
                    ),
                )
            reservation = json.loads(str(row["reservation_json"] or "{}"))
            if (
                state == "completed"
                and bool(reservation.get("_account_usage_in_parent_session"))
                and usage
            ):
                input_tokens = max(int(usage.get("input_tokens", 0) or 0), 0)
                output_tokens = max(int(usage.get("output_tokens", 0) or 0), 0)
                cost_usd = max(float(usage.get("cost_usd", 0) or 0), 0.0)
                await connection.execute(
                    """UPDATE runs SET input_tokens=input_tokens+?,
                              output_tokens=output_tokens+?,cost_usd=cost_usd+?
                       WHERE id=?""",
                    (
                        input_tokens,
                        output_tokens,
                        cost_usd,
                        str(row["parent_run_id"]),
                    ),
                )

    async def resume_attempt(self, attempt_id: str) -> None:
        timestamp = now()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT task_id,worker_id,state FROM agent_attempts WHERE id=?",
                    (attempt_id,),
                )
            ).fetchone()
            if row is None or str(row["state"]) != "suspended":
                raise ValueError("agent attempt is not suspended")
            await connection.execute(
                """UPDATE agent_attempts SET state='running',finished_at=NULL,error=NULL
                   WHERE id=? AND state='suspended'""",
                (attempt_id,),
            )
            await connection.execute(
                """UPDATE agent_tasks SET state='running',finished_at=NULL,error=NULL
                   WHERE id=? AND state='waiting_approval'""",
                (str(row["task_id"]),),
            )
            if row["worker_id"] is not None:
                await connection.execute(
                    "UPDATE agent_workers SET state='running',updated_at=? WHERE id=?",
                    (timestamp, str(row["worker_id"])),
                )

    async def prepare_resume_attempt(
        self,
        task_id: str,
        worker_id: str,
        *,
        contract_sha256: str,
    ) -> dict[str, Any]:
        """Atomically reopen one interrupted attempt without creating a new claim."""
        timestamp = now()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    """SELECT a.id AS attempt_id,a.state AS attempt_state,a.worker_id,
                              t.state AS task_state,t.assigned_worker_id,t.contract_sha256,
                              w.state AS worker_state,c.contract_sha256 AS checkpoint_contract,
                              c.child_session_id,c.child_run_id,c.resumable
                       FROM agent_tasks t
                       JOIN agent_attempts a ON a.task_id=t.id
                       JOIN agent_workers w ON w.id=a.worker_id
                       LEFT JOIN agent_checkpoints c ON c.attempt_id=a.id
                       WHERE t.id=? ORDER BY a.ordinal DESC LIMIT 1""",
                    (task_id,),
                )
            ).fetchone()
            if row is None:
                raise ValueError("agent interrupted attempt does not exist")
            if (
                str(row["worker_id"]) != worker_id
                or str(row["assigned_worker_id"]) != worker_id
            ):
                raise ValueError("agent interrupted attempt belongs to another worker")
            if (
                str(row["task_state"]) != "interrupted"
                or str(row["attempt_state"]) != "interrupted"
            ):
                raise ValueError("agent attempt is not interrupted")
            if str(row["worker_state"]) != "interrupted":
                raise ValueError("agent worker is not interrupted")
            if str(row["contract_sha256"]) != contract_sha256:
                raise ValueError("agent task contract digest does not match")
            if (
                not bool(row["resumable"])
                or not row["child_session_id"]
                or str(row["checkpoint_contract"]) != contract_sha256
            ):
                raise ValueError("Agent transcript checkpoint is not resumable")
            attempt_id = str(row["attempt_id"])
            attempt_update = await connection.execute(
                """UPDATE agent_attempts SET state='created',error=NULL,finished_at=NULL
                   WHERE id=? AND state='interrupted'""",
                (attempt_id,),
            )
            task_update = await connection.execute(
                """UPDATE agent_tasks SET state='claimed',error=NULL,finished_at=NULL
                   WHERE id=? AND state='interrupted' AND assigned_worker_id=?""",
                (task_id, worker_id),
            )
            worker_update = await connection.execute(
                """UPDATE agent_workers SET state='starting',child_session_id=?,updated_at=?
                   WHERE id=? AND state='interrupted'""",
                (str(row["child_session_id"]), timestamp, worker_id),
            )
            if (
                attempt_update.rowcount != 1
                or task_update.rowcount != 1
                or worker_update.rowcount != 1
            ):
                raise ValueError("agent resume changed concurrently")
            return {
                "attempt_id": attempt_id,
                "child_session_id": str(row["child_session_id"]),
                "child_run_id": row["child_run_id"],
            }

    async def _dependency_cycle(self, task_id: str) -> bool:
        row = await self.one(
            """WITH RECURSIVE ancestors(id) AS (
                 SELECT blocked_by_task_id FROM agent_task_dependencies WHERE task_id=?
                 UNION
                 SELECT d.blocked_by_task_id FROM agent_task_dependencies d
                 JOIN ancestors a ON d.task_id=a.id
               ) SELECT 1 FROM ancestors WHERE id=? LIMIT 1""",
            (task_id, task_id),
        )
        return row is not None

    async def mark_cleaned(self, task_id: str) -> None:
        await self.execute(
            "UPDATE agent_workspaces SET cleaned_at=?,retained=0 WHERE task_id=?",
            (now(), task_id),
        )

    async def mark_workspace_cleaned(self, path: str) -> None:
        await self.execute(
            "UPDATE agent_workspaces SET cleaned_at=?,retained=0 WHERE path=?",
            (now(), path),
        )

    async def mark_retained(self, task_id: str) -> None:
        await self.execute(
            "UPDATE agent_workspaces SET retained=1 WHERE task_id=?", (task_id,)
        )


def _mailbox(row: dict[str, Any]) -> dict[str, Any]:
    encoded = str(row.pop("payload_json"))
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(
        row["payload_sha256"]
    ):
        raise ValueError("stored mailbox message digest does not match")
    row["payload"] = json.loads(encoded)
    return row
