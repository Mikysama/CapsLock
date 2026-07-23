"""Run step, event, tool-call, and citation journal persistence."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ...domain import (
    AgentEvent,
    AgentEventKind,
    RunInfo,
    RunStepInfo,
    RunStepKind,
    RunStepStatus,
)
from .core import Repository, now
from .workflow_records import event, run, step


class RunJournalRepository(Repository):
    async def get_run(
        self, run_id: str, *, session_id: str | None = None
    ) -> RunInfo | None:
        query, values = "SELECT * FROM runs WHERE id=?", [run_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = await self.one(query, tuple(values))
        return None if row is None else run(row)

    async def create_step(self, run_id: str, kind: RunStepKind) -> RunStepInfo:
        identifier, timestamp = uuid.uuid4().hex, now()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT coalesce(max(ordinal),-1)+1 FROM run_steps WHERE run_id=?",
                    (run_id,),
                )
            ).fetchone()
            await connection.execute(
                "INSERT INTO run_steps(id,run_id,ordinal,kind,status,started_at) VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    run_id,
                    int(row[0]),
                    kind.value,
                    RunStepStatus.RUNNING.value,
                    timestamp,
                ),
            )
        return await self.require_step(identifier)

    async def finish_step(
        self,
        step_id: str,
        *,
        status: RunStepStatus,
        checkpoint: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> RunStepInfo:
        updated = await self.execute(
            "UPDATE run_steps SET status=?,checkpoint_json=?,finished_at=?,error=? WHERE id=? AND status='running'",
            (
                status.value,
                json.dumps(checkpoint, ensure_ascii=False)
                if checkpoint is not None
                else None,
                now(),
                error,
                step_id,
            ),
        )
        if not updated:
            raise ValueError("run step is not running")
        return await self.require_step(step_id)

    async def require_step(self, step_id: str) -> RunStepInfo:
        row = await self.one("SELECT * FROM run_steps WHERE id=?", (step_id,))
        if row is None:
            raise ValueError(f"run step does not exist: {step_id}")
        return step(row)

    async def last_stable_step(self, run_id: str) -> RunStepInfo | None:
        row = await self.one(
            "SELECT * FROM run_steps WHERE run_id=? AND status='completed' AND checkpoint_json IS NOT NULL ORDER BY ordinal DESC LIMIT 1",
            (run_id,),
        )
        return None if row is None else step(row)

    async def append_event(
        self, run_id: str, kind: AgentEventKind, payload: dict[str, Any]
    ) -> AgentEvent:
        async with self.database.transaction() as connection:
            return await self.append_in(connection, run_id, kind, payload)

    async def append_in(
        self, connection, run_id: str, kind: AgentEventKind, payload: dict[str, Any]
    ) -> AgentEvent:
        row = await (
            await connection.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM run_events WHERE run_id=?",
                (run_id,),
            )
        ).fetchone()
        sequence, timestamp = int(row[0]), now()
        await connection.execute(
            "INSERT INTO run_events(run_id,sequence,event_kind,payload_json,created_at) VALUES(?,?,?,?,?)",
            (
                run_id,
                sequence,
                kind.value,
                json.dumps(payload, ensure_ascii=False),
                timestamp,
            ),
        )
        parent = await (
            await connection.execute(
                "SELECT session_id,work_item_id FROM runs WHERE id=?", (run_id,)
            )
        ).fetchone()
        if parent is None:
            raise ValueError(f"run does not exist: {run_id}")
        return AgentEvent(
            sequence,
            timestamp,
            str(parent["session_id"]),
            run_id,
            str(parent["work_item_id"]),
            kind,
            payload,
        )

    async def events(self, run_id: str) -> list[AgentEvent]:
        rows = await self.all(
            """SELECT e.*,r.session_id,r.work_item_id FROM run_events e JOIN runs r ON r.id=e.run_id WHERE e.run_id=? ORDER BY e.sequence""",
            (run_id,),
        )
        return [event(row) for row in rows]

    async def record_tool_call(
        self,
        run_id: str,
        name: str,
        arguments: dict[str, Any],
        ok: bool,
        summary: str,
        duration_ms: int,
    ) -> None:
        await self.execute(
            "INSERT INTO tool_calls(run_id,name,arguments_json,ok,result_summary,duration_ms) VALUES(?,?,?,?,?,?)",
            (
                run_id,
                name,
                json.dumps(arguments, ensure_ascii=False),
                int(ok),
                summary[:1000],
                duration_ms,
            ),
        )

    async def record_citations(self, run_id: str, citations: list[Any]) -> None:
        async with self.database.transaction() as connection:
            await connection.executemany(
                "INSERT INTO citations(run_id,citation_id,path,start_line,end_line) VALUES(?,?,?,?,?)",
                [
                    (run_id, item.id, str(item.path), item.start_line, item.end_line)
                    for item in citations
                ],
            )
