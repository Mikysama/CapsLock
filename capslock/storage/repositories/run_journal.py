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


def _validate_input_answers(questions: object, answers: object) -> None:
    if not isinstance(questions, list) or not isinstance(answers, dict):
        raise ValueError("answers must be an object keyed by question id")
    expected = {
        str(question.get("id")): question
        for question in questions
        if isinstance(question, dict) and isinstance(question.get("id"), str)
    }
    if set(answers) != set(expected):
        raise ValueError("answers must contain exactly one entry per question")
    for identifier, answer in answers.items():
        question = expected[identifier]
        multiple = bool(question.get("multiple", False))
        values = answer if isinstance(answer, list) else [answer]
        if multiple != isinstance(answer, list) or not values:
            raise ValueError(f"invalid answer shape for question {identifier}")
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError(f"answers for question {identifier} must be text")
        options = {
            str(option.get("value", option.get("label")))
            for option in question.get("options", [])
            if isinstance(option, dict)
        }
        allow_free = bool(question.get("allow_free_text", True))
        if not allow_free and any(value not in options for value in values):
            raise ValueError(f"answer is not an allowed option for question {identifier}")


class RunJournalRepository(Repository):
    async def interrupt_active(self) -> None:
        """Close crash-left journal records before accepting new workspace work."""
        timestamp = now()
        preview = json.dumps(
            {
                "status": "cancelled",
                "ok": False,
                "executed": False,
                "delivery_status": "inline",
                "data": {},
                "content": [],
                "error": "workspace process ended during tool execution",
                "error_code": "process_interrupted",
            }
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                """UPDATE tool_invocations
                   SET status='cancelled',execution_status='cancelled',delivery_status='inline',
                       result_preview=?,error_code='process_interrupted',finished_at=?,
                       duration_ms=coalesce(duration_ms,0)
                   WHERE status IN ('received','validating','authorizing','queued','running')""",
                (preview, timestamp),
            )
            await connection.execute(
                """UPDATE run_steps SET status='cancelled',finished_at=?,
                       error=coalesce(error,'workspace process interrupted')
                   WHERE status='running'""",
                (timestamp,),
            )

    async def record_permission_decision(
        self,
        *,
        invocation_id: str,
        behavior: str,
        source: str,
        reason: str,
        rule: dict[str, Any] | None = None,
        classifier: dict[str, Any] | None = None,
        decided_by: str | None = None,
    ) -> str:
        identifier = f"perm_{uuid.uuid4().hex}"
        await self.execute(
            """INSERT INTO permission_decisions
               (id,invocation_id,behavior,source,reason,rule_json,classifier_json,decided_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                invocation_id,
                behavior,
                source,
                reason,
                json.dumps(rule, ensure_ascii=False) if rule is not None else None,
                json.dumps(classifier, ensure_ascii=False)
                if classifier is not None
                else None,
                decided_by,
                now(),
            ),
        )
        return identifier

    async def session_permission_rules(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.all(
            "SELECT * FROM permission_rules WHERE session_id=? ORDER BY created_at,id",
            (session_id,),
        )
        return [
            {
                "id": str(row["id"]),
                "behavior": str(row["behavior"]),
                "tool": str(row["tool"]),
                "constraints": json.loads(row["constraints_json"]),
                "source": "session",
            }
            for row in rows
        ]

    async def add_session_permission_rule(
        self,
        session_id: str,
        *,
        behavior: str,
        tool: str,
        constraints: dict[str, Any] | None = None,
    ) -> str:
        identifier = f"rule_{uuid.uuid4().hex}"
        await self.execute(
            "INSERT INTO permission_rules(id,session_id,behavior,tool,constraints_json,source,created_at) VALUES(?,?,?,?,?,'session',?)",
            (
                identifier,
                session_id,
                behavior,
                tool,
                json.dumps(constraints or {}, ensure_ascii=False),
                now(),
            ),
        )
        return identifier

    async def record_tool_discoveries(
        self, session_id: str, names: list[str], generation: int
    ) -> None:
        if not names:
            return
        async with self.database.transaction() as connection:
            await connection.executemany(
                "INSERT INTO tool_discoveries(session_id,tool_name,catalog_generation,created_at) VALUES(?,?,?,?) ON CONFLICT(session_id,tool_name) DO UPDATE SET catalog_generation=excluded.catalog_generation",
                [(session_id, name, generation, now()) for name in names],
            )

    async def tool_discoveries(self, session_id: str) -> list[str]:
        rows = await self.all(
            "SELECT tool_name FROM tool_discoveries WHERE session_id=? ORDER BY tool_name",
            (session_id,),
        )
        return [str(row["tool_name"]) for row in rows]

    async def store_result_replacement(
        self,
        *,
        tool_call_id: str,
        session_id: str,
        invocation_id: str,
        delivery_status: str,
        replacement: dict[str, Any],
    ) -> None:
        await self.execute(
            """INSERT INTO tool_result_replacements
               (tool_call_id,session_id,invocation_id,delivery_status,replacement_json,created_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(tool_call_id) DO NOTHING""",
            (
                tool_call_id,
                session_id,
                invocation_id,
                delivery_status,
                json.dumps(replacement, ensure_ascii=False),
                now(),
            ),
        )

    async def replace_tool_delivery(
        self,
        identifier: str,
        *,
        delivery_status: str,
        result_preview: str,
        artifact_id: str | None,
    ) -> None:
        await self.execute(
            "UPDATE tool_invocations SET delivery_status=?,result_preview=?,artifact_id=? WHERE id=?",
            (delivery_status, result_preview[:4096], artifact_id, identifier),
        )
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
        status: str,
        execution_status: str,
        delivery_status: str,
        result_preview: str,
        duration_ms: int,
        artifact_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self.execute(
            """UPDATE tool_invocations
               SET status=?,execution_status=?,delivery_status=?,result_preview=?,artifact_id=?,error_code=?,finished_at=?,duration_ms=?
               WHERE id=? AND status IN ('received','validating','authorizing','queued','running')""",
            (
                status,
                execution_status,
                delivery_status,
                result_preview[:4096],
                artifact_id,
                error_code,
                now(),
                duration_ms,
                identifier,
            ),
        )

    async def update_tool_invocation(
        self,
        identifier: str,
        *,
        status: str | None = None,
        policy: dict[str, Any] | None = None,
        timings: dict[str, int] | None = None,
    ) -> None:
        values: list[object] = []
        assignments: list[str] = []
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        if policy is not None:
            assignments.extend(("resolved_policy_json=?", "capabilities_json=?"))
            encoded = json.dumps(policy, ensure_ascii=False)
            values.extend((encoded, encoded))
        if timings is not None:
            assignments.append("timings_json=?")
            values.append(json.dumps(timings, ensure_ascii=False))
        if not assignments:
            return
        values.append(identifier)
        await self.execute(
            f"UPDATE tool_invocations SET {','.join(assignments)} WHERE id=?",
            tuple(values),
        )

    async def pause_tool_invocation(
        self,
        identifier: str,
        *,
        kind: str,
        request_id: str,
        continuation: dict[str, Any],
    ) -> None:
        if kind not in {"approval", "user_input"}:
            raise ValueError("invalid tool pause kind")
        status = "waiting_approval" if kind == "approval" else "waiting_input"
        updated = await self.execute(
            """UPDATE tool_invocations
               SET status=?,pause_kind=?,pause_request_id=?,continuation_json=?
               WHERE id=? AND status IN ('received','validating','authorizing','queued','running')""",
            (
                status,
                kind,
                request_id,
                json.dumps(continuation, ensure_ascii=False),
                identifier,
            ),
        )
        if not updated:
            raise ValueError("tool invocation is not pausable")

    async def pause_step(
        self, identifier: str, *, kind: str, checkpoint: dict[str, Any]
    ) -> None:
        status = "waiting_approval" if kind == "approval" else "waiting_input"
        updated = await self.execute(
            "UPDATE run_steps SET status=?,checkpoint_json=? WHERE id=? AND status='running'",
            (status, json.dumps(checkpoint, ensure_ascii=False), identifier),
        )
        if not updated:
            raise ValueError("run step is not pausable")

    async def update_step_checkpoint(
        self, identifier: str, checkpoint: dict[str, Any]
    ) -> None:
        updated = await self.execute(
            """UPDATE run_steps SET checkpoint_json=? WHERE id=?
               AND status IN ('waiting_approval','waiting_input')""",
            (json.dumps(checkpoint, ensure_ascii=False), identifier),
        )
        if not updated:
            raise ValueError("paused run step does not exist")

    async def create_input_request(
        self,
        *,
        request_id: str,
        session_id: str,
        run_id: str,
        invocation_id: str,
        questions: object,
        resume_data: dict[str, object],
    ) -> None:
        await self.execute(
            """INSERT INTO tool_input_requests(
                 id,session_id,run_id,invocation_id,status,questions_json,
                 resume_data_json,created_at
               ) VALUES(?,?,?,?, 'pending',?,?,?)""",
            (
                request_id,
                session_id,
                run_id,
                invocation_id,
                json.dumps(questions, ensure_ascii=False),
                json.dumps(resume_data, ensure_ascii=False),
                now(),
            ),
        )

    async def list_input_requests(
        self, session_id: str, *, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM tool_input_requests WHERE session_id=?"
        values: list[object] = [session_id]
        if status is not None:
            query += " AND status=?"
            values.append(status)
        query += " ORDER BY created_at,id"
        return [
            {
                "id": str(row["id"]),
                "session_id": str(row["session_id"]),
                "run_id": str(row["run_id"]),
                "invocation_id": str(row["invocation_id"]),
                "status": str(row["status"]),
                "questions": json.loads(row["questions_json"]),
                "answers": json.loads(row["answers_json"])
                if row["answers_json"] is not None
                else None,
                "created_at": str(row["created_at"]),
            }
            for row in await self.all(query, tuple(values))
        ]

    async def answer_input_request(
        self, request_id: str, session_id: str, answers: object
    ) -> dict[str, Any]:
        row = await self.one(
            "SELECT * FROM tool_input_requests WHERE id=? AND session_id=?",
            (request_id, session_id),
        )
        if row is None:
            raise ValueError("input request does not exist in this session")
        if row["status"] != "pending":
            raise ValueError("input request has already been resolved")
        questions = json.loads(row["questions_json"])
        _validate_input_answers(questions, answers)
        await self.execute(
            """UPDATE tool_input_requests SET status='answered',answers_json=?,answered_at=?
               WHERE id=? AND session_id=? AND status='pending'""",
            (json.dumps(answers, ensure_ascii=False), now(), request_id, session_id),
        )
        return {
            "id": request_id,
            "status": "answered",
            "answers": answers,
            "run_id": str(row["run_id"]),
            "invocation_id": str(row["invocation_id"]),
        }

    async def cancel_input_request(
        self, request_id: str, session_id: str
    ) -> dict[str, Any]:
        updated = await self.execute(
            """UPDATE tool_input_requests SET status='cancelled',answered_at=?
               WHERE id=? AND session_id=? AND status='pending'""",
            (now(), request_id, session_id),
        )
        if not updated:
            raise ValueError("pending input request does not exist in this session")
        return {"id": request_id, "status": "cancelled"}

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
