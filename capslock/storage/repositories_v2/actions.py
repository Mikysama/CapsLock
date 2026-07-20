"""Unified v2 action persistence and state transitions."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ...domain import ActionRecord, ActionResultKind, ActionStatus, ActionType
from .core import Repository, now


class ActionRepository(Repository):
    async def create(
        self,
        *,
        session_id: str,
        run_id: str,
        action_type: ActionType,
        summary: str,
        request: dict[str, Any],
    ) -> ActionRecord:
        identifier, created = uuid.uuid4().hex, now()
        await self.execute(
            """INSERT INTO actions(id,session_id,run_id,action_type,status,summary,request_json,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                identifier,
                session_id,
                run_id,
                action_type.value,
                ActionStatus.PENDING.value,
                summary,
                json.dumps(request, ensure_ascii=False),
                created,
            ),
        )
        return await self.require(identifier, session_id=session_id)

    async def get(
        self, action_id: str, *, session_id: str | None = None
    ) -> ActionRecord | None:
        query, values = "SELECT * FROM actions WHERE id=?", [action_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = await self.one(query, tuple(values))
        return None if row is None else _action(row)

    async def require(
        self, action_id: str, *, session_id: str | None = None
    ) -> ActionRecord:
        action = await self.get(action_id, session_id=session_id)
        if action is None:
            raise ValueError("action does not belong to this session or does not exist")
        return action

    async def resolve(
        self, session_id: str, prefix: str, *, types: set[ActionType] | None = None
    ) -> ActionRecord:
        if not prefix:
            raise ValueError("provide an action id")
        query = "SELECT * FROM actions WHERE session_id=? AND (id=? OR id LIKE ?)"
        values: list[object] = [session_id, prefix, f"{prefix}%"]
        if types:
            query += " AND action_type IN (" + ",".join("?" for _ in types) + ")"
            values.extend(
                item.value for item in sorted(types, key=lambda item: item.value)
            )
        query += " ORDER BY created_at LIMIT 2"
        rows = await self.all(query, tuple(values))
        if len(rows) > 1:
            raise ValueError("action id prefix is ambiguous; provide more characters")
        if not rows:
            raise ValueError("action does not belong to this session or does not exist")
        return _action(rows[0])

    async def list(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        types: set[ActionType] | None = None,
        statuses: set[ActionStatus] | None = None,
    ) -> list[ActionRecord]:
        query, values = "SELECT * FROM actions WHERE session_id=?", [session_id]
        if run_id is not None:
            query += " AND run_id=?"
            values.append(run_id)
        if types:
            query += " AND action_type IN (" + ",".join("?" for _ in types) + ")"
            values.extend(
                item.value for item in sorted(types, key=lambda item: item.value)
            )
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            values.extend(
                item.value for item in sorted(statuses, key=lambda item: item.value)
            )
        query += " ORDER BY created_at"
        return [_action(row) for row in await self.all(query, tuple(values))]

    async def set_risk(
        self, action_id: str, *, level: str, reason: str, rollback: str
    ) -> ActionRecord:
        updated = await self.execute(
            "UPDATE actions SET risk_level=?,risk_reason=?,rollback=? WHERE id=?",
            (level, reason, rollback, action_id),
        )
        if not updated:
            raise ValueError(f"action does not exist: {action_id}")
        return await self.require(action_id)

    async def transition(
        self,
        action_id: str,
        target: ActionStatus,
        *,
        result: dict[str, Any] | None = None,
        result_kind: ActionResultKind | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ActionRecord:
        action = await self.require(action_id)
        allowed = {
            ActionStatus.PENDING: {
                ActionStatus.APPROVED,
                ActionStatus.REJECTED,
                ActionStatus.CANCELLED,
            },
            ActionStatus.APPROVED: {ActionStatus.RUNNING, ActionStatus.CANCELLED},
            ActionStatus.RUNNING: {
                ActionStatus.COMPLETED,
                ActionStatus.FAILED,
                ActionStatus.CANCELLED,
            },
        }
        if target not in allowed.get(action.status, set()):
            raise ValueError(
                f"invalid action transition: {action.status.value} -> {target.value}"
            )
        timestamp = now()
        fields = [
            "status=?",
            "result_json=?",
            "result_kind=?",
            "error_code=?",
            "error_message=?",
        ]
        values: list[object] = [
            target.value,
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            result_kind.value if result_kind else None,
            error_code,
            error_message,
        ]
        if target is ActionStatus.APPROVED:
            fields.extend(("approved_at=?", "decided_at=?"))
            values.extend((timestamp, timestamp))
        elif target is ActionStatus.RUNNING:
            fields.append("started_at=?")
            values.append(timestamp)
        else:
            fields.append("finished_at=?")
            values.append(timestamp)
            if target in {ActionStatus.REJECTED, ActionStatus.CANCELLED}:
                fields.append("decided_at=coalesce(decided_at,?)")
                values.append(timestamp)
        values.extend((action_id, action.status.value))
        updated = await self.execute(
            f"UPDATE actions SET {','.join(fields)} WHERE id=? AND status=?",
            tuple(values),
        )
        if not updated:
            raise ValueError("action changed concurrently")
        return await self.require(action_id)

    async def cancel_run(self, run_id: str, *, error_message: str) -> int:
        timestamp = now()
        return await self.execute(
            """UPDATE actions SET status='cancelled',result_kind='user_cancelled',finished_at=?,
               decided_at=coalesce(decided_at,?),error_code='cancelled',error_message=?
               WHERE run_id=? AND status IN ('pending','approved','running')""",
            (timestamp, timestamp, error_message, run_id),
        )

    async def last_completed_file_action(self, session_id: str) -> ActionRecord | None:
        row = await self.one(
            """SELECT * FROM actions WHERE session_id=? AND action_type IN ('file_edit','file_create')
               AND status='completed' AND result_kind='applied' AND reversed_at IS NULL
               ORDER BY finished_at DESC LIMIT 1""",
            (session_id,),
        )
        return None if row is None else _action(row)

    async def mark_reversed(self, action_id: str) -> ActionRecord:
        updated = await self.execute(
            "UPDATE actions SET result_kind='undone',reversed_at=? WHERE id=? AND status='completed' AND reversed_at IS NULL",
            (now(), action_id),
        )
        if not updated:
            raise ValueError("action cannot be reversed")
        return await self.require(action_id)


def _action(row) -> ActionRecord:
    return ActionRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        run_id=str(row["run_id"]),
        type=ActionType(row["action_type"]),
        status=ActionStatus(row["status"]),
        summary=str(row["summary"]),
        request=json.loads(row["request_json"]),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        result_kind=ActionResultKind(row["result_kind"])
        if row["result_kind"]
        else None,
        created_at=str(row["created_at"]),
        approved_at=row["approved_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        reversed_at=row["reversed_at"],
        decided_at=row["decided_at"],
        risk_level=row["risk_level"],
        risk_reason=row["risk_reason"],
        rollback=row["rollback"],
        error_code=row["error_code"],
        error_message=row["error_message"],
    )
