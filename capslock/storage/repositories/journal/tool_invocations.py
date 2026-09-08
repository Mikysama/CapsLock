"""Tool invocation and delivery journal component."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ....domain import AgentEvent
from ..core import now


class ToolInvocationJournalRepository:
    async def recent_working_set(
        self, session_id: str, limit: int = 5
    ) -> list[dict[str, object]]:
        rows = await self.all(
            """SELECT id,arguments_json,result_preview FROM tool_invocations
               WHERE session_id=? AND name='read_file' AND status='completed'
               ORDER BY finished_at DESC,sequence DESC LIMIT ?""",
            (session_id, max(1, min(limit * 4, 80))),
        )
        output: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in rows:
            try:
                arguments = json.loads(row["arguments_json"])
                preview = json.loads(row["result_preview"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(preview, dict) and isinstance(preview.get("data"), dict):
                preview = preview["data"]
            path = preview.get("path") if isinstance(preview, dict) else None
            if not isinstance(path, str):
                path = arguments.get("path") if isinstance(arguments, dict) else None
            if not isinstance(path, str) or path in seen:
                continue
            seen.add(path)
            digest = preview.get("sha256") if isinstance(preview, dict) else None
            details = [f"invocation {row['id']}"]
            start = preview.get("start_line") if isinstance(preview, dict) else None
            end = preview.get("end_line") if isinstance(preview, dict) else None
            if not isinstance(start, int) and isinstance(arguments, dict):
                start = arguments.get("start_line")
            if not isinstance(end, int) and isinstance(arguments, dict):
                end = arguments.get("end_line")
            if isinstance(start, int) or isinstance(end, int):
                details.append(f"lines {start or 1}-{end or 'end'}")
            output.append(
                {
                    "kind": "file",
                    "identifier": path,
                    "digest": digest if isinstance(digest, str) else None,
                    "details": details,
                    "source_refs": [f"tool:{row['id']}"],
                }
            )
            if len(output) >= limit:
                break
        return output

    async def tool_invocation(self, identifier: str) -> dict[str, Any] | None:
        row = await self.one("SELECT * FROM tool_invocations WHERE id=?", (identifier,))
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "run_id": str(row["run_id"]),
            "session_id": str(row["session_id"]),
            "name": str(row["name"]),
            "tool_call_id": str(row["tool_call_id"]),
            "resolved_policy": json.loads(row["resolved_policy_json"]),
            "arguments": json.loads(row["arguments_json"]),
            "status": str(row["status"]),
        }

    async def tool_invocation_for_call(
        self, run_id: str, tool_call_id: str
    ) -> dict[str, Any] | None:
        row = await self.one(
            "SELECT id FROM tool_invocations WHERE run_id=? AND tool_call_id=?",
            (run_id, tool_call_id),
        )
        return None if row is None else await self.tool_invocation(str(row["id"]))

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
        updated = await self.execute(
            """UPDATE tool_invocations SET delivered_result_json=?
               WHERE id=? AND session_id=? AND tool_call_id=?""",
            (
                json.dumps(replacement, ensure_ascii=False),
                invocation_id,
                session_id,
                tool_call_id,
            ),
        )
        if not updated:
            raise ValueError("tool result delivery has no matching invocation")

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
        updated = await self.execute(
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
        if updated and self.episodic is not None and result_preview:
            row = await self.one(
                "SELECT session_id,run_id FROM tool_invocations WHERE id=?",
                (identifier,),
            )
            if row is not None:
                await self.episodic.index(
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    source_kind="tool_result",
                    source_id=identifier,
                    content=result_preview,
                    artifact_id=artifact_id,
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
        async with self.database.transaction() as connection:
            updated = await connection.execute(
                "UPDATE run_steps SET status=?,checkpoint_json=? WHERE id=? AND status='running'",
                (status, json.dumps(checkpoint, ensure_ascii=False), identifier),
            )
            if not updated.rowcount:
                raise ValueError("run step is not pausable")
            await connection.execute(
                """UPDATE run_steps AS previous SET checkpoint_json=NULL
                   WHERE previous.run_id=(SELECT run_id FROM run_steps WHERE id=?)
                     AND previous.id<>? AND previous.checkpoint_json IS NOT NULL
                     AND previous.status NOT IN ('waiting_approval','waiting_input')
                     AND NOT EXISTS (
                       SELECT 1 FROM runs r WHERE r.resume_from_step_id=previous.id
                     )
                     AND previous.ordinal<(SELECT ordinal FROM run_steps WHERE id=?)""",
                (identifier, identifier, identifier),
            )

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

    async def record_tool_call(
        self,
        run_id: str,
        name: str,
        arguments: dict[str, Any],
        ok: bool,
        summary: str,
        duration_ms: int,
        *,
        invocation_id: str,
    ) -> None:
        await self.execute(
            """INSERT INTO tool_calls(
                 run_id,invocation_id,name,arguments_json,ok,result_summary,duration_ms
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                run_id,
                invocation_id,
                name,
                json.dumps(arguments, ensure_ascii=False),
                int(ok),
                summary[:1000],
                duration_ms,
            ),
        )
