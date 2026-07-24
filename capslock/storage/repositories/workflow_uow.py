"""Atomic workflow state transitions spanning multiple tables."""

from __future__ import annotations

import json
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

    async def pause(
        self,
        run_id: str,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        if kind not in {"approval", "user_input"}:
            raise ValueError("unsupported run pause kind")
        status = "waiting_approval" if kind == "approval" else "waiting_input"
        event_kind = (
            AgentEventKind.WAITING_APPROVAL
            if kind == "approval"
            else AgentEventKind.WAITING_INPUT
        )
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
                "UPDATE runs SET status=? WHERE id=? AND status='running'",
                (status, run_id),
            )
            updated = await connection.execute(
                """UPDATE work_items SET status=?,updated_at=?
                   WHERE id=? AND status='running'""",
                (status, timestamp, str(row["work_item_id"])),
            )
            if not updated.rowcount:
                raise ValueError("work item is not running")
            return await self._append_event(connection, run_id, event_kind, payload)

    async def resume_paused(self, session_id: str, run_id: str) -> None:
        async with self.database.transaction() as connection:
            run_row = await (
                await connection.execute(
                    """SELECT work_item_id,status FROM runs
                       WHERE id=? AND session_id=?
                       AND status IN ('waiting_approval','waiting_input')""",
                    (run_id, session_id),
                )
            ).fetchone()
            if run_row is None:
                raise ValueError("run is not waiting for resumable input")
            invocation = await (
                await connection.execute(
                    """SELECT * FROM tool_invocations WHERE run_id=?
                       AND status IN ('waiting_approval','waiting_input')
                       ORDER BY sequence DESC LIMIT 1""",
                    (run_id,),
                )
            ).fetchone()
            step = await (
                await connection.execute(
                    """SELECT * FROM run_steps WHERE run_id=?
                       AND status IN ('waiting_approval','waiting_input')
                       ORDER BY ordinal DESC LIMIT 1""",
                    (run_id,),
                )
            ).fetchone()
            if invocation is None or step is None:
                raise ValueError("paused run continuation is incomplete")
            pause_kind = str(invocation["pause_kind"])
            request_id = str(invocation["pause_request_id"])
            status, executed = "succeeded", True
            data: object = {}
            error = error_code = None
            if pause_kind == "user_input":
                request = await (
                    await connection.execute(
                        """SELECT * FROM tool_input_requests
                           WHERE id=? AND invocation_id=?""",
                        (request_id, str(invocation["id"])),
                    )
                ).fetchone()
                if request is None or request["status"] == "pending":
                    raise ValueError("user input request has not been answered")
                if request["status"] == "cancelled":
                    status, executed = "cancelled", False
                    error, error_code = (
                        "user cancelled the input request",
                        "input_cancelled",
                    )
                else:
                    data = {"answers": json.loads(request["answers_json"])}
            else:
                action = await (
                    await connection.execute(
                        "SELECT * FROM actions WHERE id=? AND run_id=?",
                        (request_id, run_id),
                    )
                ).fetchone()
                if action is None or action["status"] in {
                    "pending",
                    "approved",
                    "running",
                }:
                    raise ValueError("action approval has not reached a terminal state")
                data = {
                    "action_id": str(action["id"]),
                    "kind": str(action["action_type"]),
                    "summary": str(action["summary"]),
                    "status": str(action["status"]),
                    "result_kind": action["result_kind"],
                    "request": json.loads(action["request_json"]),
                    "result": json.loads(action["result_json"])
                    if action["result_json"]
                    else None,
                    "error": action["error_message"],
                }
                if action["status"] == "rejected":
                    status, executed = "denied", False
                    error, error_code = (
                        action["error_message"] or "action was rejected",
                        "action_rejected",
                    )
                elif action["status"] == "cancelled":
                    status, executed = "cancelled", False
                    error, error_code = (
                        action["error_message"] or "action was cancelled",
                        "action_cancelled",
                    )
                elif action["status"] == "failed":
                    status, executed = "failed", True
                    error, error_code = (
                        action["error_message"] or "action failed",
                        action["error_code"] or "action_failed",
                    )
            result = json.dumps(
                {
                    "status": status,
                    "ok": status == "succeeded",
                    "executed": executed,
                    "delivery_status": "inline",
                    "data": data,
                    "content": [],
                    "error": error,
                    "error_code": error_code,
                },
                ensure_ascii=False,
            )
            checkpoint = json.loads(step["checkpoint_json"] or "{}")
            messages = list(checkpoint.get("messages", []))
            tool_message = {
                "role": "tool",
                "tool_call_id": str(invocation["tool_call_id"]),
                "content": result,
            }
            assistant_calls = next(
                (
                    item.get("tool_calls", [])
                    for item in reversed(messages)
                    if isinstance(item, dict)
                    and item.get("role") == "assistant"
                    and isinstance(item.get("tool_calls"), list)
                ),
                [],
            )
            ordered_ids = [
                str(item.get("id"))
                for item in assistant_calls
                if isinstance(item, dict)
            ]
            paused_id = str(invocation["tool_call_id"])
            later_ids = (
                set(ordered_ids[ordered_ids.index(paused_id) + 1 :])
                if paused_id in ordered_ids
                else set()
            )
            insertion = next(
                (
                    index
                    for index, item in enumerate(messages)
                    if isinstance(item, dict)
                    and item.get("role") == "tool"
                    and item.get("tool_call_id") in later_ids
                ),
                len(messages),
            )
            messages.insert(insertion, tool_message)
            timestamp = now()
            terminal_status = (
                "completed"
                if status == "succeeded"
                else "cancelled"
                if status == "cancelled"
                else "failed"
            )
            await connection.execute(
                """UPDATE tool_invocations SET status=?,execution_status=?,
                   delivery_status='inline',result_preview=?,error_code=?,
                   finished_at=?,duration_ms=coalesce(duration_ms,0)
                   WHERE id=?""",
                (
                    terminal_status,
                    status,
                    result,
                    error_code,
                    timestamp,
                    str(invocation["id"]),
                ),
            )
            await connection.execute(
                """UPDATE run_steps SET status=?,checkpoint_json=?,finished_at=?,error=?
                   WHERE id=?""",
                (
                    "completed",
                    json.dumps({"messages": messages}, ensure_ascii=False),
                    timestamp,
                    error,
                    str(step["id"]),
                ),
            )
            await connection.execute(
                "UPDATE runs SET status='running',finished_at=NULL WHERE id=?",
                (run_id,),
            )
            await connection.execute(
                "UPDATE work_items SET status='running',updated_at=? WHERE id=?",
                (timestamp, str(run_row["work_item_id"])),
            )

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
            paused_invocation = await (
                await connection.execute(
                    """SELECT 1 FROM tool_invocations WHERE run_id=?
                       AND status='waiting_approval' LIMIT 1""",
                    (run_id,),
                )
            ).fetchone()
            if paused_invocation is not None:
                # Tool Runtime v2 resumes this same run after the caller has
                # committed the approval decision.
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
