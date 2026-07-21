"""Async workflow, run, checkpoint, and event persistence."""

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
    SessionTitleSource,
    WorkItemInfo,
    WorkItemStatus,
    normalize_session_title,
)
from .core import Repository, now


class WorkflowRepository(Repository):
    async def enqueue(
        self, session_id: str, question: str, *, parent_work_item_id: str | None = None
    ) -> WorkItemInfo:
        identifier, timestamp = uuid.uuid4().hex, now()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT coalesce(max(position),-1)+1 FROM work_items WHERE session_id=? AND status='queued'",
                    (session_id,),
                )
            ).fetchone()
            position = int(row[0])
            await connection.execute(
                """INSERT INTO work_items(id,session_id,question,status,position,parent_work_item_id,created_at,updated_at)
                   VALUES(?,?,?,'queued',?,?,?,?)""",
                (
                    identifier,
                    session_id,
                    question,
                    position,
                    parent_work_item_id,
                    timestamp,
                    timestamp,
                ),
            )
        return await self.require_work_item(identifier)

    async def get_work_item(self, item_id: str) -> WorkItemInfo | None:
        row = await self.one(
            """SELECT w.*,(SELECT r.id FROM runs r WHERE r.work_item_id=w.id ORDER BY r.started_at DESC LIMIT 1) current_run_id
               FROM work_items w WHERE w.id=?""",
            (item_id,),
        )
        return None if row is None else _work_item(row)

    async def require_work_item(self, item_id: str) -> WorkItemInfo:
        item = await self.get_work_item(item_id)
        if item is None:
            raise ValueError(f"work item does not exist: {item_id}")
        return item

    async def list_work_items(
        self, session_id: str, *, active_only: bool = False
    ) -> list[WorkItemInfo]:
        query = """SELECT w.*,(SELECT r.id FROM runs r WHERE r.work_item_id=w.id ORDER BY r.started_at DESC LIMIT 1) current_run_id
                   FROM work_items w WHERE w.session_id=?"""
        values: list[object] = [session_id]
        if active_only:
            query += " AND w.status IN ('queued','running','waiting_approval')"
        query += " ORDER BY w.position,w.created_at"
        return [_work_item(row) for row in await self.all(query, tuple(values))]

    async def update_work_item(
        self, item_id: str, status: WorkItemStatus, *, error: str | None = None
    ) -> WorkItemInfo:
        current = await self.require_work_item(item_id)
        allowed = {
            WorkItemStatus.QUEUED: {WorkItemStatus.RUNNING, WorkItemStatus.CANCELLED},
            WorkItemStatus.RUNNING: {
                WorkItemStatus.WAITING_APPROVAL,
                WorkItemStatus.COMPLETED,
                WorkItemStatus.FAILED,
                WorkItemStatus.CANCELLED,
                WorkItemStatus.INTERRUPTED,
                WorkItemStatus.STOPPED,
            },
            WorkItemStatus.WAITING_APPROVAL: {
                WorkItemStatus.COMPLETED,
                WorkItemStatus.FAILED,
                WorkItemStatus.CANCELLED,
            },
        }
        if status is not current.status and status not in allowed.get(
            current.status, set()
        ):
            raise ValueError(
                f"invalid work item transition: {current.status.value} -> {status.value}"
            )
        updated = await self.execute(
            "UPDATE work_items SET status=?,error=?,updated_at=? WHERE id=? AND status=?",
            (status.value, error, now(), item_id, current.status.value),
        )
        if not updated:
            raise ValueError("work item changed concurrently")
        return await self.require_work_item(item_id)

    async def reorder(self, item_id: str, position: int) -> WorkItemInfo:
        item = await self.require_work_item(item_id)
        if item.status is not WorkItemStatus.QUEUED:
            raise ValueError("only queued work items can be reordered")
        await self.execute(
            "UPDATE work_items SET position=?,updated_at=? WHERE id=?",
            (max(0, position), now(), item_id),
        )
        return await self.require_work_item(item_id)

    async def start_run(
        self,
        session_id: str,
        work_item_id: str,
        question: str,
        *,
        parent_run_id: str | None = None,
        resume_from_step_id: str | None = None,
    ) -> RunInfo:
        identifier, started = uuid.uuid4().hex, now()
        try:
            title = normalize_session_title(question, truncate=True)
        except ValueError:
            title = None
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE work_items SET status='running',error=NULL,updated_at=? WHERE id=? AND session_id=? AND status='queued'",
                (started, work_item_id, session_id),
            )
            if not cursor.rowcount:
                raise ValueError(
                    "work item is not queued or does not belong to the session"
                )
            await connection.execute(
                """INSERT INTO runs(id,session_id,work_item_id,question,status,started_at,parent_run_id,resume_from_step_id)
                   VALUES(?,?,?,?,'running',?,?,?)""",
                (
                    identifier,
                    session_id,
                    work_item_id,
                    question,
                    started,
                    parent_run_id,
                    resume_from_step_id,
                ),
            )
            if title:
                await connection.execute(
                    """UPDATE sessions SET title=?,title_source=?,title_updated_at=?,updated_at=?
                       WHERE id=? AND title_source=?""",
                    (
                        title,
                        SessionTitleSource.FIRST_QUESTION.value,
                        started,
                        started,
                        session_id,
                        SessionTitleSource.PENDING.value,
                    ),
                )
        return await self.require_run(identifier)

    async def get_run(
        self, run_id: str, *, session_id: str | None = None
    ) -> RunInfo | None:
        query, values = "SELECT * FROM runs WHERE id=?", [run_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = await self.one(query, tuple(values))
        return None if row is None else _run(row)

    async def require_run(
        self, run_id: str, *, session_id: str | None = None
    ) -> RunInfo:
        item = await self.get_run(run_id, session_id=session_id)
        if item is None:
            raise ValueError(f"run does not exist: {run_id}")
        return item

    async def retryable_run(self, session_id: str, prefix: str) -> RunInfo:
        rows = await self.all(
            """SELECT * FROM runs WHERE session_id=? AND substr(id,1,?)=?
               AND status IN ('failed','cancelled','interrupted','stopped') ORDER BY started_at DESC LIMIT 2""",
            (session_id, len(prefix), prefix),
        )
        if len(rows) > 1:
            raise ValueError("run id prefix is ambiguous")
        if not rows:
            raise ValueError("retryable run does not exist in this session")
        run = _run(rows[0])
        if await self.last_stable_step(run.id) is None:
            raise ValueError("run has no stable checkpoint")
        return run

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        duration_ms: int,
        error_code: str | None = None,
        error_message: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0,
        stop_reason: str | None = None,
    ) -> RunInfo:
        allowed = {
            "waiting_approval",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "stopped",
        }
        if status not in allowed:
            raise ValueError(f"unsupported terminal run status: {status}")
        updated = await self.execute(
            """UPDATE runs SET status=?,finished_at=?,duration_ms=?,error_code=?,error_message=?,
               input_tokens=?,output_tokens=?,cost_usd=?,stop_reason=? WHERE id=? AND status='running'""",
            (
                status,
                now(),
                duration_ms,
                error_code,
                error_message,
                input_tokens,
                output_tokens,
                cost_usd,
                stop_reason,
                run_id,
            ),
        )
        if not updated:
            raise ValueError("run is not running")
        return await self.require_run(run_id)

    async def finalize(
        self,
        run_id: str,
        *,
        status: WorkItemStatus,
        event_kind: AgentEventKind,
        payload: dict[str, Any],
        duration_ms: int,
        error_code: str | None = None,
        error_message: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0,
        stop_reason: str | None = None,
    ) -> AgentEvent:
        run_status = status.value
        if status not in {
            WorkItemStatus.WAITING_APPROVAL,
            WorkItemStatus.COMPLETED,
            WorkItemStatus.FAILED,
            WorkItemStatus.CANCELLED,
            WorkItemStatus.INTERRUPTED,
            WorkItemStatus.STOPPED,
        }:
            raise ValueError(f"unsupported workflow final status: {status.value}")
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT work_item_id FROM runs WHERE id=? AND status='running'",
                    (run_id,),
                )
            ).fetchone()
            if row is None:
                raise ValueError("run is not running")
            timestamp = now()
            await connection.execute(
                """UPDATE runs SET status=?,finished_at=?,duration_ms=?,error_code=?,error_message=?,
                   input_tokens=?,output_tokens=?,cost_usd=?,stop_reason=? WHERE id=? AND status='running'""",
                (
                    run_status,
                    timestamp,
                    duration_ms,
                    error_code,
                    error_message,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    stop_reason,
                    run_id,
                ),
            )
            work_item = await connection.execute(
                "UPDATE work_items SET status=?,error=?,updated_at=? WHERE id=? AND status='running'",
                (status.value, error_message, timestamp, str(row["work_item_id"])),
            )
            if not work_item.rowcount:
                raise ValueError("work item is not running")
            step_status = (
                RunStepStatus.CANCELLED.value
                if status is WorkItemStatus.CANCELLED
                else RunStepStatus.FAILED.value
            )
            if status in {
                WorkItemStatus.CANCELLED,
                WorkItemStatus.FAILED,
                WorkItemStatus.INTERRUPTED,
                WorkItemStatus.STOPPED,
            }:
                await connection.execute(
                    """UPDATE run_steps SET status=?,finished_at=?,error=coalesce(error,?)
                       WHERE run_id=? AND status='running'""",
                    (step_status, timestamp, error_message, run_id),
                )
            if status in {
                WorkItemStatus.CANCELLED,
                WorkItemStatus.FAILED,
                WorkItemStatus.INTERRUPTED,
                WorkItemStatus.STOPPED,
            }:
                action_status = (
                    "cancelled" if status is WorkItemStatus.CANCELLED else "failed"
                )
                result_kind = (
                    "user_cancelled"
                    if status is WorkItemStatus.CANCELLED
                    else "execution_error"
                )
                await connection.execute(
                    """UPDATE actions SET status=?,result_kind=?,finished_at=?,error_code=?,error_message=?
                       WHERE run_id=? AND status IN ('pending','approved','running')""",
                    (
                        action_status,
                        result_kind,
                        timestamp,
                        error_code,
                        error_message,
                        run_id,
                    ),
                )
            return await self._append_event(connection, run_id, event_kind, payload)

    async def run_completed(self, run_id: str) -> bool:
        row = await self.one("SELECT status FROM runs WHERE id=?", (run_id,))
        return row is not None and str(row[0]) == "completed"

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
            """UPDATE run_steps SET status=?,checkpoint_json=?,finished_at=?,error=?
               WHERE id=? AND status='running'""",
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
        return _step(row)

    async def last_stable_step(self, run_id: str) -> RunStepInfo | None:
        row = await self.one(
            "SELECT * FROM run_steps WHERE run_id=? AND status='completed' AND checkpoint_json IS NOT NULL ORDER BY ordinal DESC LIMIT 1",
            (run_id,),
        )
        return None if row is None else _step(row)

    async def append_event(
        self, run_id: str, kind: AgentEventKind, payload: dict[str, Any]
    ) -> AgentEvent:
        async with self.database.transaction() as connection:
            return await self._append_event(connection, run_id, kind, payload)

    async def _append_event(
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
        run = await (
            await connection.execute(
                "SELECT session_id,work_item_id FROM runs WHERE id=?", (run_id,)
            )
        ).fetchone()
        if run is None:
            raise ValueError(f"run does not exist: {run_id}")
        return AgentEvent(
            sequence,
            timestamp,
            str(run["session_id"]),
            run_id,
            str(run["work_item_id"]),
            kind,
            payload,
        )

    async def events(self, run_id: str) -> list[AgentEvent]:
        rows = await self.all(
            """SELECT e.*,r.session_id,r.work_item_id FROM run_events e JOIN runs r ON r.id=e.run_id
               WHERE e.run_id=? ORDER BY e.sequence""",
            (run_id,),
        )
        return [_event(row) for row in rows]

    async def settle_approval(self, session_id: str, run_id: str) -> AgentEvent | None:
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT work_item_id,status FROM runs WHERE id=? AND session_id=?",
                    (run_id, session_id),
                )
            ).fetchone()
            if row is None:
                raise ValueError("run does not belong to this session")
            if row["status"] in {"completed", "failed", "cancelled"}:
                return None
            if row["status"] != "waiting_approval":
                raise ValueError("run is not waiting for approval")
            pending = await (
                await connection.execute(
                    "SELECT 1 FROM actions WHERE run_id=? AND status IN ('pending','approved','running') LIMIT 1",
                    (run_id,),
                )
            ).fetchone()
            if pending is not None:
                return None
            failed = await (
                await connection.execute(
                    """SELECT status,error_code,error_message FROM actions
                       WHERE run_id=? AND status IN ('failed','cancelled')
                       ORDER BY finished_at LIMIT 1""",
                    (run_id,),
                )
            ).fetchone()
            timestamp = now()
            if failed is None:
                status = WorkItemStatus.COMPLETED
                kind = AgentEventKind.COMPLETED
                payload: dict[str, Any] = {
                    "approval_settled": True,
                    "status": status.value,
                }
                error_code = error_message = None
            else:
                cancelled = failed["status"] == "cancelled"
                status = (
                    WorkItemStatus.CANCELLED if cancelled else WorkItemStatus.FAILED
                )
                kind = AgentEventKind.CANCELLED if cancelled else AgentEventKind.FAILED
                error_code = str(failed["error_code"] or status.value)
                error_message = str(failed["error_message"] or f"action {status.value}")
                payload = {
                    "approval_settled": True,
                    "status": status.value,
                    "error": {"code": error_code, "message": error_message},
                }
            await connection.execute(
                """UPDATE runs SET status=?,finished_at=coalesce(finished_at,?),
                   error_code=?,error_message=? WHERE id=? AND status='waiting_approval'""",
                (status.value, timestamp, error_code, error_message, run_id),
            )
            work_item = await connection.execute(
                """UPDATE work_items SET status=?,error=?,updated_at=?
                   WHERE id=? AND status='waiting_approval'""",
                (
                    status.value,
                    error_message,
                    timestamp,
                    str(row["work_item_id"]),
                ),
            )
            if not work_item.rowcount:
                raise ValueError("work item is not waiting for approval")
            return await self._append_event(connection, run_id, kind, payload)

    async def cancel_waiting_action(
        self, session_id: str, run_id: str, action_id: str, *, message: str
    ) -> AgentEvent:
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    """SELECT work_item_id FROM runs
                       WHERE id=? AND session_id=? AND status='waiting_approval'""",
                    (run_id, session_id),
                )
            ).fetchone()
            if row is None:
                raise ValueError("run is not waiting for approval")
            timestamp = now()
            action = await connection.execute(
                """UPDATE actions SET status='cancelled',result_kind='user_cancelled',
                   finished_at=?,decided_at=coalesce(decided_at,?),error_code='cancelled',
                   error_message=? WHERE id=? AND run_id=? AND status IN ('approved','running')""",
                (timestamp, timestamp, message, action_id, run_id),
            )
            if not action.rowcount:
                raise ValueError("action is not approved or running")
            await connection.execute(
                """UPDATE actions SET status='cancelled',result_kind='user_cancelled',
                   finished_at=?,decided_at=coalesce(decided_at,?),error_code='cancelled',
                   error_message=? WHERE run_id=? AND status IN ('pending','approved')""",
                (timestamp, timestamp, message, run_id),
            )
            await connection.execute(
                """UPDATE runs SET status='cancelled',error_code='cancelled',
                   error_message=?,finished_at=coalesce(finished_at,?) WHERE id=?""",
                (message, timestamp, run_id),
            )
            await connection.execute(
                """UPDATE work_items SET status='cancelled',error=?,updated_at=?
                   WHERE id=? AND status='waiting_approval'""",
                (message, timestamp, str(row["work_item_id"])),
            )
            return await self._append_event(
                connection,
                run_id,
                AgentEventKind.CANCELLED,
                {
                    "status": "cancelled",
                    "error": {"code": "cancelled", "message": message},
                },
            )

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

    async def session_cost(self, session_id: str) -> tuple[int, int, float]:
        row = await self.one(
            "SELECT coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),coalesce(sum(cost_usd),0) FROM runs WHERE session_id=?",
            (session_id,),
        )
        return (int(row[0]), int(row[1]), float(row[2])) if row else (0, 0, 0.0)


def _work_item(row) -> WorkItemInfo:
    return WorkItemInfo(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        question=str(row["question"]),
        status=WorkItemStatus(row["status"]),
        position=int(row["position"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        current_run_id=row["current_run_id"],
        parent_work_item_id=row["parent_work_item_id"],
        error=row["error"],
    )


def _run(row) -> RunInfo:
    return RunInfo(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        work_item_id=str(row["work_item_id"]),
        question=str(row["question"]),
        status=str(row["status"]),
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        cost_usd=float(row["cost_usd"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        parent_run_id=row["parent_run_id"],
        resume_from_step_id=row["resume_from_step_id"],
        stop_reason=row["stop_reason"],
    )


def _step(row) -> RunStepInfo:
    return RunStepInfo(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        ordinal=int(row["ordinal"]),
        kind=RunStepKind(row["kind"]),
        status=RunStepStatus(row["status"]),
        checkpoint=json.loads(row["checkpoint_json"])
        if row["checkpoint_json"]
        else None,
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
        error=row["error"],
    )


def _event(row) -> AgentEvent:
    return AgentEvent(
        int(row["sequence"]),
        str(row["created_at"]),
        str(row["session_id"]),
        str(row["run_id"]),
        str(row["work_item_id"]),
        AgentEventKind(row["event_kind"]),
        json.loads(row["payload_json"]),
    )
