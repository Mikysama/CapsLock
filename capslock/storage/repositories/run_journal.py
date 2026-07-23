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
    async def event_state(self, run_id: str) -> tuple[int, str, str, str]:
        row = await self.one(
            """SELECT r.session_id,r.work_item_id,
                      coalesce(max(e.sequence),0) AS sequence,
                      coalesce(max(e.trace_id),'') AS trace_id
               FROM runs r LEFT JOIN run_events e ON e.run_id=r.id
               WHERE r.id=? GROUP BY r.id""",
            (run_id,),
        )
        if row is None:
            raise ValueError(f"run does not exist: {run_id}")
        return (
            int(row["sequence"]),
            str(row["session_id"]),
            str(row["work_item_id"]),
            str(row["trace_id"]),
        )

    async def append_prepared_events(self, events: list[AgentEvent]) -> None:
        if not events:
            return
        async with self.database.transaction() as connection:
            await connection.executemany(
                """INSERT INTO run_events(run_id,sequence,event_id,trace_id,event_kind,payload_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        item.run_id,
                        item.sequence,
                        item.event_id,
                        item.trace_id,
                        item.kind.value,
                        json.dumps(item.data, ensure_ascii=False),
                        item.timestamp,
                    )
                    for item in events
                ],
            )

    async def start_tool_invocation(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        name: str,
        spec: dict[str, Any],
        capabilities: dict[str, Any],
        arguments: dict[str, Any],
        status: str = "running",
    ) -> str:
        identifier = f"inv_{uuid.uuid4().hex}"
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT coalesce(max(sequence),0)+1 FROM tool_invocations WHERE run_id=?",
                    (run_id,),
                )
            ).fetchone()
            await connection.execute(
                """INSERT INTO tool_invocations(id,run_id,session_id,sequence,tool_call_id,name,spec_json,capabilities_json,arguments_json,status,started_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    run_id,
                    session_id,
                    int(row[0]),
                    tool_call_id,
                    name,
                    json.dumps(spec, ensure_ascii=False),
                    json.dumps(capabilities, ensure_ascii=False),
                    json.dumps(arguments, ensure_ascii=False),
                    status,
                    now(),
                ),
            )
        return identifier

    async def finish_tool_invocation(
        self,
        identifier: str,
        *,
        ok: bool,
        result_preview: str,
        duration_ms: int,
        artifact_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self.execute(
            """UPDATE tool_invocations
               SET status=?,result_preview=?,artifact_id=?,error_code=?,finished_at=?,duration_ms=?
               WHERE id=? AND status IN ('validating','authorizing','running')""",
            (
                "completed" if ok else "failed",
                result_preview[:4096],
                artifact_id,
                error_code,
                now(),
                duration_ms,
                identifier,
            ),
        )

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
        trace_row = await (
            await connection.execute(
                "SELECT trace_id FROM run_events WHERE run_id=? ORDER BY sequence LIMIT 1",
                (run_id,),
            )
        ).fetchone()
        event_id = f"evt_{uuid.uuid4().hex}"
        trace_id = (
            str(trace_row[0])
            if trace_row is not None and str(trace_row[0])
            else f"trace_{uuid.uuid4().hex}"
        )
        await connection.execute(
            "INSERT INTO run_events(run_id,sequence,event_id,trace_id,event_kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                run_id,
                sequence,
                event_id,
                trace_id,
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
            event_id,
            trace_id,
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
