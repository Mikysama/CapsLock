"""Permission rule, decision, and approval journal component."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..core import now


class PermissionJournalRepository:
    async def record_permission_decision(
        self,
        *,
        invocation_id: str,
        behavior: str,
        source: str,
        reason: str,
        reason_code: str = "legacy_decision",
        mode: str = "approve_for_me",
        arguments_sha256: str = "",
        rule: dict[str, Any] | None = None,
        classifier: dict[str, Any] | None = None,
        decided_by: str | None = None,
        suggestions: list[dict[str, Any]] | None = None,
    ) -> str:
        identifier = f"perm_{uuid.uuid4().hex}"
        await self.execute(
            """INSERT INTO permission_decisions
               (id,invocation_id,behavior,source,reason,reason_code,mode,arguments_sha256,
                rule_json,classifier_json,suggestions_json,decided_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                invocation_id,
                behavior,
                source,
                reason,
                reason_code,
                mode,
                arguments_sha256,
                json.dumps(rule, ensure_ascii=False) if rule is not None else None,
                json.dumps(classifier, ensure_ascii=False)
                if classifier is not None
                else None,
                json.dumps(suggestions or [], ensure_ascii=False),
                decided_by,
                now(),
            ),
        )
        return identifier

    async def recent_permission_decisions(
        self, session_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = await self.all(
            """SELECT d.*,i.name AS tool FROM permission_decisions d
               JOIN tool_invocations i ON i.id=d.invocation_id
               WHERE i.session_id=? ORDER BY d.created_at DESC LIMIT ?""",
            (session_id, max(1, min(int(limit), 200))),
        )
        return [
            {
                "id": str(row["id"]),
                "tool": str(row["tool"]),
                "behavior": str(row["behavior"]),
                "source": str(row["source"]),
                "reason": str(row["reason"]),
                "reason_code": str(row["reason_code"]),
                "mode": str(row["mode"]),
                "arguments_sha256": str(row["arguments_sha256"]),
                "rule": json.loads(row["rule_json"]) if row["rule_json"] else None,
                "classifier": json.loads(row["classifier_json"])
                if row["classifier_json"]
                else None,
                "decided_by": row["decided_by"],
                "suggestions": json.loads(row["suggestions_json"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

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
                "matcher_version": int(row["matcher_version"]),
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
        matcher_version: int = 2,
    ) -> str:
        identifier = f"rule_{uuid.uuid4().hex}"
        await self.execute(
            "INSERT INTO permission_rules(id,session_id,behavior,tool,constraints_json,source,matcher_version,created_at) VALUES(?,?,?,?,?,'session',?,?)",
            (
                identifier,
                session_id,
                behavior,
                tool,
                json.dumps(constraints or {}, ensure_ascii=False),
                matcher_version,
                now(),
            ),
        )
        return identifier

    async def remove_session_permission_rule(
        self, session_id: str, identifier: str
    ) -> None:
        removed = await self.execute(
            "DELETE FROM permission_rules WHERE id=? AND session_id=?",
            (identifier, session_id),
        )
        if not removed:
            raise ValueError("session permission rule does not exist")

    async def permission_setting(self, key: str) -> str | None:
        row = await self.one("SELECT value FROM workspace_settings WHERE key=?", (key,))
        return None if row is None else str(row[0])

    async def set_permission_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO workspace_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def create_permission_request(
        self,
        *,
        session_id: str,
        run_id: str,
        invocation_id: str,
        tool: str,
        arguments_sha256: str,
        reason: str,
        suggestions: list[dict[str, Any]],
    ) -> str:
        identifier = f"permission_{uuid.uuid4().hex}"
        await self.execute(
            """INSERT INTO permission_requests(
                 id,session_id,run_id,invocation_id,tool,arguments_sha256,reason,
                 suggestions_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                session_id,
                run_id,
                invocation_id,
                tool,
                arguments_sha256,
                reason,
                json.dumps(suggestions, ensure_ascii=False),
                now(),
            ),
        )
        return identifier

    async def permission_request(
        self, identifier: str, *, session_id: str | None = None
    ) -> dict[str, Any] | None:
        query, values = "SELECT * FROM permission_requests WHERE id=?", [identifier]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = await self.one(query, tuple(values))
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "session_id": str(row["session_id"]),
            "run_id": str(row["run_id"]),
            "invocation_id": str(row["invocation_id"]),
            "tool": str(row["tool"]),
            "arguments_sha256": str(row["arguments_sha256"]),
            "reason": str(row["reason"]),
            "suggestions": json.loads(row["suggestions_json"]),
            "status": str(row["status"]),
            "choice": row["choice"],
            "selected_update": json.loads(row["selected_update_json"])
            if row["selected_update_json"]
            else None,
            "feedback": row["feedback"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": str(row["created_at"]),
            "decided_at": row["decided_at"],
        }

    async def list_permission_requests(
        self, session_id: str, *, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        query, values = (
            "SELECT id FROM permission_requests WHERE session_id=?",
            [session_id],
        )
        if status is not None:
            query += " AND status=?"
            values.append(status)
        query += " ORDER BY created_at"
        rows = await self.all(query, tuple(values))
        result: list[dict[str, Any]] = []
        for row in rows:
            item = await self.permission_request(str(row["id"]), session_id=session_id)
            if item is not None:
                result.append(item)
        return result

    async def decide_permission_request(
        self,
        identifier: str,
        *,
        session_id: str,
        choice: str,
        selected_update: dict[str, Any] | None = None,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        if choice not in {"approve_once", "approve_session", "approve_local", "reject"}:
            raise ValueError("invalid permission approval choice")
        request = await self.permission_request(identifier, session_id=session_id)
        if request is None or request["status"] != "pending":
            raise ValueError("permission request is not pending")
        target = "rejected" if choice == "reject" else "approved"
        async with self.database.transaction() as connection:
            updated = await connection.execute(
                """UPDATE permission_requests SET status=?,choice=?,selected_update_json=?,
                     feedback=?,decided_at=? WHERE id=? AND session_id=? AND status='pending'""",
                (
                    target,
                    choice,
                    json.dumps(selected_update, ensure_ascii=False)
                    if selected_update is not None
                    else None,
                    feedback,
                    now(),
                    identifier,
                    session_id,
                ),
            )
            if not updated.rowcount:
                raise ValueError("permission request changed concurrently")
            if target == "approved":
                await connection.execute(
                    """INSERT INTO permission_grants(
                         id,permission_request_id,session_id,run_id,invocation_id,tool,
                         arguments_sha256,created_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        f"grant_{uuid.uuid4().hex}",
                        identifier,
                        session_id,
                        request["run_id"],
                        request["invocation_id"],
                        request["tool"],
                        request["arguments_sha256"],
                        now(),
                    ),
                )
        decided = await self.permission_request(identifier, session_id=session_id)
        assert decided is not None
        return decided

    async def complete_permission_request(
        self, identifier: str, *, session_id: str, result: dict[str, Any]
    ) -> None:
        updated = await self.execute(
            """UPDATE permission_requests SET result_json=? WHERE id=? AND session_id=?
               AND status IN ('approved','rejected','cancelled')""",
            (json.dumps(result, ensure_ascii=False), identifier, session_id),
        )
        if not updated:
            raise ValueError("decided permission request does not exist")

    async def consume_permission_grant(
        self,
        *,
        session_id: str,
        run_id: str,
        invocation_id: str,
        tool: str,
        arguments_sha256: str,
    ) -> bool:
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    """SELECT id FROM permission_grants WHERE session_id=? AND run_id=?
                       AND invocation_id=? AND tool=? AND arguments_sha256=?
                       AND consumed_at IS NULL""",
                    (session_id, run_id, invocation_id, tool, arguments_sha256),
                )
            ).fetchone()
            if row is None:
                return False
            updated = await connection.execute(
                "UPDATE permission_grants SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (now(), str(row["id"])),
            )
            return bool(updated.rowcount)
