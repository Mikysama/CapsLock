"""Persistence for parent/child collaboration contracts and audit records."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ...collaboration.models import (
    AgentMessage,
    AgentTaskContract,
    AgentTaskState,
    ValidatedAgentOutput,
)
from .core import Repository, now


class CollaborationRepository(Repository):
    async def interrupt_active(self) -> int:
        return await self.execute(
            """UPDATE agent_tasks SET state='interrupted',error='interrupted during process restart',
               finished_at=? WHERE state IN ('created','running','waiting_approval')""",
            (now(),),
        )

    async def create_task(
        self,
        contract: AgentTaskContract,
        *,
        workspace_path: str | None = None,
        source_path: str = "",
    ) -> None:
        timestamp = now()
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO agent_tasks(id,parent_run_id,objective,contract_json,state,child_workspace,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    contract.task_id,
                    contract.parent_run_id,
                    contract.objective,
                    json.dumps(contract.as_dict(), ensure_ascii=False),
                    AgentTaskState.CREATED.value,
                    None,
                    timestamp,
                ),
            )
            for ordinal, capability in enumerate(contract.capabilities):
                await connection.execute(
                    "INSERT INTO agent_capabilities(task_id,ordinal,capability_json) VALUES(?,?,?)",
                    (contract.task_id, ordinal, json.dumps(capability.as_dict())),
                )
            if workspace_path is not None:
                await connection.execute(
                    "INSERT INTO agent_workspaces(task_id,path,source_path,created_at) VALUES(?,?,?,?)",
                    (contract.task_id, workspace_path, source_path, timestamp),
                )

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
                AgentTaskState.RUNNING,
                AgentTaskState.FAILED,
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
        await self.execute(
            """INSERT INTO agent_outputs(task_id,state,output_json,verified,output_sha256,created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                output.task_id,
                output.state.value,
                encoded,
                int(output.verified),
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                now(),
            ),
        )

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
            error=value.get("error"),
        )

    async def list_tasks(self, parent_run_id: str) -> list[dict[str, Any]]:
        rows = await self.all(
            "SELECT * FROM agent_tasks WHERE parent_run_id=? ORDER BY created_at,id",
            (parent_run_id,),
        )
        return [dict(row) for row in rows]

    async def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.all(
            """SELECT t.* FROM agent_tasks t JOIN runs r ON r.id=t.parent_run_id
               WHERE r.session_id=? ORDER BY t.created_at,t.id""",
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

    async def mark_cleaned(self, task_id: str) -> None:
        await self.execute(
            "UPDATE agent_workspaces SET cleaned_at=?,retained=0 WHERE task_id=?",
            (now(), task_id),
        )

    async def mark_retained(self, task_id: str) -> None:
        await self.execute(
            "UPDATE agent_workspaces SET retained=1 WHERE task_id=?", (task_id,)
        )
