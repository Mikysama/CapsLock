"""Atomic workflow state transitions spanning multiple tables."""

from __future__ import annotations

import uuid
from typing import Any

from ...domain import (
    AgentEvent,
    AgentEventKind,
    RunInfo,
    SessionTitleSource,
    WorkItemStatus,
    approval_outcome,
    interrupted_step_status,
    normalize_session_title,
    validate_final_status,
)
from .core import Repository, now
from .run_journal import RunJournalRepository
from .runs import RunRepository


class WorkflowUnitOfWork(Repository):
    """Own the transaction boundaries for compound workflow transitions."""

    def __init__(
        self,
        database,
        runs: RunRepository,
        journal: RunJournalRepository,
    ) -> None:
        super().__init__(database)
        self.runs = runs
        self.journal = journal

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
                "UPDATE work_items SET status='running',error=NULL,updated_at=? "
                "WHERE id=? AND session_id=? AND status='queued'",
                (started, work_item_id, session_id),
            )
            if not cursor.rowcount:
                raise ValueError(
                    "work item is not queued or does not belong to the session"
                )
            await connection.execute(
                """INSERT INTO runs(
                       id,session_id,work_item_id,question,status,started_at,
                       parent_run_id,resume_from_step_id
                   ) VALUES(?,?,?,?,'running',?,?,?)""",
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
                    """UPDATE sessions
                       SET title=?,title_source=?,title_updated_at=?,updated_at=?
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
        return await self.runs.require(identifier)

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
        validate_final_status(status)
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
                """UPDATE runs SET status=?,finished_at=?,duration_ms=?,
                   error_code=?,error_message=?,input_tokens=?,output_tokens=?,
                   cost_usd=?,stop_reason=? WHERE id=? AND status='running'""",
                (
                    status.value,
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
                """UPDATE work_items SET status=?,error=?,updated_at=?
                   WHERE id=? AND status='running'""",
                (status.value, error_message, timestamp, str(row["work_item_id"])),
            )
            if not work_item.rowcount:
                raise ValueError("work item is not running")
            step_status = interrupted_step_status(status)
            if step_status is not None:
                await connection.execute(
                    """UPDATE run_steps SET status=?,finished_at=?,error=coalesce(error,?)
                       WHERE run_id=? AND status='running'""",
                    (step_status.value, timestamp, error_message, run_id),
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
                    """UPDATE actions SET status=?,result_kind=?,finished_at=?,
                       error_code=?,error_message=? WHERE run_id=?
                       AND status IN ('pending','approved','running')""",
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
                    """SELECT 1 FROM actions WHERE run_id=?
                       AND status IN ('pending','approved','running') LIMIT 1""",
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
            outcome = approval_outcome(
                None if failed is None else str(failed["status"]),
                None if failed is None else failed["error_code"],
                None if failed is None else failed["error_message"],
            )
            status, kind = outcome.status, outcome.event_kind
            if failed is None:
                payload: dict[str, Any] = {
                    "approval_settled": True,
                    "status": status.value,
                }
                error_code = error_message = None
            else:
                error_code = outcome.error_code
                error_message = outcome.error_message
                payload = {
                    "approval_settled": True,
                    "status": status.value,
                    "error": {"code": error_code, "message": error_message},
                }
            await connection.execute(
                """UPDATE runs SET status=?,finished_at=coalesce(finished_at,?),
                   error_code=?,error_message=?
                   WHERE id=? AND status='waiting_approval'""",
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
        self,
        session_id: str,
        run_id: str,
        action_id: str,
        *,
        message: str,
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
                """UPDATE actions SET status='cancelled',
                   result_kind='user_cancelled',finished_at=?,
                   decided_at=coalesce(decided_at,?),error_code='cancelled',
                   error_message=? WHERE id=? AND run_id=?
                   AND status IN ('approved','running')""",
                (timestamp, timestamp, message, action_id, run_id),
            )
            if not action.rowcount:
                raise ValueError("action is not approved or running")
            await connection.execute(
                """UPDATE actions SET status='cancelled',
                   result_kind='user_cancelled',finished_at=?,
                   decided_at=coalesce(decided_at,?),error_code='cancelled',
                   error_message=? WHERE run_id=?
                   AND status IN ('pending','approved')""",
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

    async def _append_event(
        self,
        connection,
        run_id: str,
        kind: AgentEventKind,
        payload: dict[str, Any],
    ) -> AgentEvent:
        """Transaction-local event seam used by rollback tests."""

        return await self.journal.append_in(connection, run_id, kind, payload)
