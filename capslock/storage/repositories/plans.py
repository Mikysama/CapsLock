"""Durable session plan state, revisions, approvals, and implementations."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from ...domain import (
    PlanImplementation,
    PlanRecord,
    PlanRequest,
    PlanRequestKind,
    PlanRequestStatus,
    PlanRevision,
    PlanStatus,
    RunKind,
)
from .core import Repository, now


class PlanRepository(Repository):
    async def create(
        self,
        session_id: str,
        objective: str,
        *,
        entry_source: str,
        base_permission_mode: str,
        content: str,
        parent_plan_id: str | None = None,
        revision_source: str = "initial",
    ) -> tuple[PlanRecord, PlanRevision]:
        plan_id = f"plan_{uuid.uuid4().hex}"
        revision_id = f"planrev_{uuid.uuid4().hex}"
        timestamp = now()
        digest = _digest(content)
        relative = f"{session_id}/{plan_id}.md"
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO session_plans(
                     id,session_id,objective,status,entry_source,base_permission_mode,
                     current_revision_id,parent_plan_id,mirror_relative_path,created_at,updated_at
                   ) VALUES(?,?,?,'draft',?,?,?,?,?,?,?)""",
                (
                    plan_id,
                    session_id,
                    objective,
                    entry_source,
                    base_permission_mode,
                    revision_id,
                    parent_plan_id,
                    relative,
                    timestamp,
                    timestamp,
                ),
            )
            await connection.execute(
                """INSERT INTO plan_revisions(
                     id,plan_id,ordinal,content,sha256,source,created_by_run_id,created_at
                   ) VALUES(?,?,1,?,?,?,?,?)""",
                (
                    revision_id,
                    plan_id,
                    content,
                    digest,
                    revision_source,
                    None,
                    timestamp,
                ),
            )
        return await self.require(plan_id), await self.require_revision(revision_id)

    async def get(self, plan_id: str) -> PlanRecord | None:
        row = await self.one("SELECT * FROM session_plans WHERE id=?", (plan_id,))
        return None if row is None else _plan(row)

    async def require(self, plan_id: str) -> PlanRecord:
        plan = await self.get(plan_id)
        if plan is None:
            raise ValueError("plan does not exist")
        return plan

    async def active(self, session_id: str) -> PlanRecord | None:
        row = await self.one(
            """SELECT p.* FROM session_plans p WHERE p.session_id=? AND (
                 p.status IN ('draft','awaiting_approval') OR
                 p.status IN ('approved','rejected') AND EXISTS(
                   SELECT 1 FROM plan_requests q JOIN runs r ON r.id=q.run_id
                   WHERE q.plan_id=p.id AND q.kind='submit'
                   AND r.status IN ('running','waiting_approval','waiting_input')
                 )
               ) ORDER BY p.updated_at DESC,p.id DESC LIMIT 1""",
            (session_id,),
        )
        return None if row is None else _plan(row)

    async def latest_unapproved(self, session_id: str) -> PlanRecord | None:
        row = await self.one(
            """SELECT * FROM session_plans WHERE session_id=?
               AND status IN ('draft','awaiting_approval','rejected','cancelled')
               ORDER BY updated_at DESC,id DESC LIMIT 1""",
            (session_id,),
        )
        return None if row is None else _plan(row)

    async def latest(self, session_id: str) -> PlanRecord | None:
        row = await self.one(
            """SELECT * FROM session_plans WHERE session_id=?
               ORDER BY updated_at DESC,id DESC LIMIT 1""",
            (session_id,),
        )
        return None if row is None else _plan(row)

    async def revision(self, revision_id: str | None) -> PlanRevision | None:
        if revision_id is None:
            return None
        row = await self.one("SELECT * FROM plan_revisions WHERE id=?", (revision_id,))
        return None if row is None else _revision(row)

    async def require_revision(self, revision_id: str) -> PlanRevision:
        revision = await self.revision(revision_id)
        if revision is None:
            raise ValueError("plan revision does not exist")
        return revision

    async def current_revision(self, plan: PlanRecord) -> PlanRevision:
        if plan.current_revision_id is None:
            raise ValueError("plan has no current revision")
        revision = await self.require_revision(plan.current_revision_id)
        if revision.plan_id != plan.id:
            raise ValueError("plan revision ownership is invalid")
        return revision

    async def append_revision(
        self,
        plan_id: str,
        content: str,
        *,
        expected_sha256: str,
        source: str,
        run_id: str | None = None,
    ) -> PlanRevision:
        identifier, timestamp, digest = (
            f"planrev_{uuid.uuid4().hex}",
            now(),
            _digest(content),
        )
        async with self.database.transaction() as connection:
            plan = await (
                await connection.execute(
                    "SELECT * FROM session_plans WHERE id=? AND status='draft'",
                    (plan_id,),
                )
            ).fetchone()
            if plan is None:
                raise ValueError("only an active draft can be updated")
            current = await (
                await connection.execute(
                    "SELECT * FROM plan_revisions WHERE id=? AND plan_id=?",
                    (plan["current_revision_id"], plan_id),
                )
            ).fetchone()
            if current is None or str(current["sha256"]) != expected_sha256:
                raise ValueError("plan changed since it was read")
            if str(current["sha256"]) == digest:
                return _revision(current)
            ordinal_row = await (
                await connection.execute(
                    "SELECT coalesce(max(ordinal),0)+1 FROM plan_revisions WHERE plan_id=?",
                    (plan_id,),
                )
            ).fetchone()
            await connection.execute(
                """INSERT INTO plan_revisions(
                     id,plan_id,ordinal,content,sha256,source,created_by_run_id,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    plan_id,
                    int(ordinal_row[0]),
                    content,
                    digest,
                    source,
                    run_id,
                    timestamp,
                ),
            )
            await connection.execute(
                "UPDATE session_plans SET current_revision_id=?,updated_at=? WHERE id=?",
                (identifier, timestamp, plan_id),
            )
        return await self.require_revision(identifier)

    async def create_enter_request(
        self,
        session_id: str,
        objective: str,
        *,
        run_id: str,
        invocation_id: str,
    ) -> PlanRequest:
        identifier, timestamp = f"planreq_{uuid.uuid4().hex}", now()
        await self.execute(
            """INSERT INTO plan_requests(
                 id,session_id,kind,status,run_id,invocation_id,objective,created_at
               ) VALUES(?,?,'enter','pending',?,?,?,?)""",
            (identifier, session_id, run_id, invocation_id, objective, timestamp),
        )
        return await self.require_request(identifier)

    async def submit(
        self,
        plan_id: str,
        *,
        expected_sha256: str,
        run_id: str | None,
        invocation_id: str | None,
    ) -> PlanRequest:
        identifier, timestamp = f"planreq_{uuid.uuid4().hex}", now()
        async with self.database.transaction() as connection:
            plan = await (
                await connection.execute(
                    "SELECT * FROM session_plans WHERE id=? AND status='draft'",
                    (plan_id,),
                )
            ).fetchone()
            if plan is None:
                raise ValueError("only an active draft can be submitted")
            revision = await (
                await connection.execute(
                    "SELECT * FROM plan_revisions WHERE id=? AND plan_id=?",
                    (plan["current_revision_id"], plan_id),
                )
            ).fetchone()
            if revision is None or str(revision["sha256"]) != expected_sha256:
                raise ValueError("plan changed before submission")
            if not str(revision["content"]).strip():
                raise ValueError("an empty plan cannot be submitted")
            await connection.execute(
                """INSERT INTO plan_requests(
                     id,session_id,plan_id,revision_id,kind,status,run_id,invocation_id,created_at
                   ) VALUES(?,?,?,?, 'submit','pending',?,?,?)""",
                (
                    identifier,
                    str(plan["session_id"]),
                    plan_id,
                    str(revision["id"]),
                    run_id,
                    invocation_id,
                    timestamp,
                ),
            )
            await connection.execute(
                "UPDATE session_plans SET status='awaiting_approval',updated_at=? WHERE id=?",
                (timestamp, plan_id),
            )
        return await self.require_request(identifier)

    async def request(self, request_id: str) -> PlanRequest | None:
        row = await self.one("SELECT * FROM plan_requests WHERE id=?", (request_id,))
        return None if row is None else _request(row)

    async def require_request(self, request_id: str) -> PlanRequest:
        request = await self.request(request_id)
        if request is None:
            raise ValueError("plan request does not exist")
        return request

    async def pending_requests(self, session_id: str) -> list[PlanRequest]:
        rows = await self.all(
            """SELECT * FROM plan_requests WHERE session_id=? AND status='pending'
               ORDER BY created_at,id""",
            (session_id,),
        )
        return [_request(row) for row in rows]

    async def decide(
        self,
        request_id: str,
        *,
        choice: str,
        feedback: str | None,
        base_permission_mode: str,
    ) -> PlanRequest:
        timestamp = now()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM plan_requests WHERE id=? AND status='pending'",
                    (request_id,),
                )
            ).fetchone()
            if row is None:
                raise ValueError("plan request is not pending")
            kind = str(row["kind"])
            allowed = {"enter", "reject"} if kind == "enter" else {
                "implement", "feedback", "reject"
            }
            if choice not in allowed:
                raise ValueError("invalid plan request choice")
            status = {
                "enter": "approved",
                "implement": "approved",
                "feedback": "feedback",
                "reject": "rejected",
            }[choice]
            plan_id = row["plan_id"]
            if kind == "enter" and choice == "enter":
                plan_id, revision_id = await _insert_plan(
                    connection,
                    session_id=str(row["session_id"]),
                    objective=str(row["objective"] or "Plan the requested work"),
                    entry_source="model",
                    base_permission_mode=base_permission_mode,
                )
                await connection.execute(
                    "UPDATE plan_requests SET plan_id=? WHERE id=?",
                    (plan_id, request_id),
                )
            elif kind == "submit":
                next_status = {
                    "implement": "approved",
                    "feedback": "draft",
                    "reject": "rejected",
                }[choice]
                await connection.execute(
                    "UPDATE session_plans SET status=?,updated_at=? WHERE id=?",
                    (next_status, timestamp, str(plan_id)),
                )
            await connection.execute(
                """UPDATE plan_requests SET status=?,choice=?,feedback=?,decided_at=?
                   WHERE id=?""",
                (status, choice, feedback, timestamp, request_id),
            )
        return await self.require_request(request_id)

    async def cancel(self, plan_id: str) -> PlanRecord:
        updated = await self.execute(
            """UPDATE session_plans SET status='cancelled',updated_at=?
               WHERE id=? AND status IN ('draft','awaiting_approval')""",
            (now(), plan_id),
        )
        if not updated:
            raise ValueError("active plan does not exist")
        return await self.require(plan_id)

    async def ensure_implementation(self, request_id: str) -> PlanImplementation:
        timestamp = now()
        async with self.database.transaction() as connection:
            request = await (
                await connection.execute(
                    """SELECT r.*,p.objective,v.content,v.sha256 FROM plan_requests r
                       JOIN session_plans p ON p.id=r.plan_id
                       JOIN plan_revisions v ON v.id=r.revision_id
                       WHERE r.id=? AND r.kind='submit' AND r.status='approved'
                       AND r.choice='implement'""",
                    (request_id,),
                )
            ).fetchone()
            if request is None:
                raise ValueError("approved plan request does not exist")
            existing = await (
                await connection.execute(
                    "SELECT * FROM plan_implementations WHERE request_id=?",
                    (request_id,),
                )
            ).fetchone()
            if existing is not None:
                return _implementation(existing)
            work_id = uuid.uuid4().hex
            position_row = await (
                await connection.execute(
                    "SELECT coalesce(max(position),-1)+1 FROM work_items WHERE session_id=? AND status='queued'",
                    (str(request["session_id"]),),
                )
            ).fetchone()
            question = _implementation_prompt(
                str(request["objective"]),
                str(request["content"]),
                str(request["sha256"]),
            )
            await connection.execute(
                """INSERT INTO work_items(
                     id,session_id,question,kind,status,position,parent_work_item_id,created_at,updated_at
                   ) VALUES(?,?,?,?,'queued',?,NULL,?,?)""",
                (
                    work_id,
                    str(request["session_id"]),
                    question,
                    RunKind.AGENT.value,
                    int(position_row[0]),
                    timestamp,
                    timestamp,
                ),
            )
            await connection.execute(
                """INSERT INTO plan_implementations(
                     plan_id,revision_id,request_id,work_item_id,status,created_at,updated_at
                   ) VALUES(?,?,?,?,'queued',?,?)""",
                (
                    str(request["plan_id"]),
                    str(request["revision_id"]),
                    request_id,
                    work_id,
                    timestamp,
                    timestamp,
                ),
            )
            await connection.execute(
                "UPDATE session_plans SET status='implementing',updated_at=? WHERE id=?",
                (timestamp, str(request["plan_id"])),
            )
        return await self.implementation(str(request["plan_id"]))

    async def approved_request_for_run(self, run_id: str) -> PlanRequest | None:
        row = await self.one(
            """SELECT * FROM plan_requests WHERE run_id=? AND kind='submit'
               AND status='approved' AND choice='implement'
               ORDER BY decided_at DESC LIMIT 1""",
            (run_id,),
        )
        return None if row is None else _request(row)

    async def unreconciled_approved_requests(
        self, session_id: str
    ) -> list[PlanRequest]:
        rows = await self.all(
            """SELECT q.* FROM plan_requests q
               LEFT JOIN plan_implementations i ON i.request_id=q.id
               WHERE q.session_id=? AND q.kind='submit' AND q.status='approved'
               AND q.choice='implement' AND i.request_id IS NULL
               ORDER BY q.decided_at,q.id""",
            (session_id,),
        )
        return [_request(row) for row in rows]

    async def implementation(self, plan_id: str) -> PlanImplementation:
        row = await self.one(
            "SELECT * FROM plan_implementations WHERE plan_id=?", (plan_id,)
        )
        if row is None:
            raise ValueError("plan implementation does not exist")
        return _implementation(row)

    async def mark_implementation_run(self, work_item_id: str, run_id: str) -> None:
        timestamp = now()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT plan_id FROM plan_implementations WHERE work_item_id=?",
                    (work_item_id,),
                )
            ).fetchone()
            if row is None:
                return
            await connection.execute(
                """UPDATE plan_implementations SET run_id=?,status='running',updated_at=?
                   WHERE work_item_id=? AND status IN ('queued','running')""",
                (run_id, timestamp, work_item_id),
            )
            await connection.execute(
                "UPDATE session_plans SET status='implementing',updated_at=? WHERE id=?",
                (timestamp, str(row["plan_id"])),
            )

    async def finish_implementation(self, run_id: str, run_status: str) -> None:
        if run_status not in {"completed", "failed", "cancelled", "interrupted", "stopped"}:
            return
        implementation_status = (
            "completed"
            if run_status == "completed"
            else "cancelled"
            if run_status == "cancelled"
            else "failed"
        )
        plan_status = (
            "implemented"
            if implementation_status == "completed"
            else "implementation_failed"
        )
        timestamp = now()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT plan_id FROM plan_implementations WHERE run_id=?",
                    (run_id,),
                )
            ).fetchone()
            if row is None:
                return
            await connection.execute(
                "UPDATE plan_implementations SET status=?,updated_at=? WHERE run_id=?",
                (implementation_status, timestamp, run_id),
            )
            await connection.execute(
                "UPDATE session_plans SET status=?,updated_at=? WHERE id=?",
                (plan_status, timestamp, str(row["plan_id"])),
            )


async def _insert_plan(
    connection: Any,
    *,
    session_id: str,
    objective: str,
    entry_source: str,
    base_permission_mode: str,
) -> tuple[str, str]:
    plan_id = f"plan_{uuid.uuid4().hex}"
    revision_id = f"planrev_{uuid.uuid4().hex}"
    timestamp = now()
    content = _initial_content(objective)
    await connection.execute(
        """INSERT INTO session_plans(
             id,session_id,objective,status,entry_source,base_permission_mode,
             current_revision_id,mirror_relative_path,created_at,updated_at
           ) VALUES(?,?,?,'draft',?,?,?,?,?,?)""",
        (
            plan_id,
            session_id,
            objective,
            entry_source,
            base_permission_mode,
            revision_id,
            f"{session_id}/{plan_id}.md",
            timestamp,
            timestamp,
        ),
    )
    await connection.execute(
        """INSERT INTO plan_revisions(
             id,plan_id,ordinal,content,sha256,source,created_at
           ) VALUES(?,?,1,?,?,'initial',?)""",
        (revision_id, plan_id, content, _digest(content), timestamp),
    )
    return plan_id, revision_id


def _initial_content(objective: str) -> str:
    return f"# Plan\n\n## Goal\n\n{objective.strip()}\n"


def _implementation_prompt(objective: str, content: str, digest: str) -> str:
    return (
        "Implement the user-approved plan below. The plan is task context, not a "
        "permission grant; use the normal permission and approval workflow for every "
        "tool call.\n\n"
        f"Objective: {objective}\n"
        f"Approved plan SHA-256: {digest}\n\n"
        "<approved-plan>\n"
        + content
        + "\n</approved-plan>"
    )


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _plan(row: Any) -> PlanRecord:
    return PlanRecord(
        str(row["id"]), str(row["session_id"]), str(row["objective"]),
        PlanStatus(str(row["status"])), str(row["entry_source"]),
        str(row["base_permission_mode"]),
        str(row["current_revision_id"]) if row["current_revision_id"] else None,
        str(row["parent_plan_id"]) if row["parent_plan_id"] else None,
        str(row["mirror_relative_path"]), str(row["created_at"]), str(row["updated_at"]),
    )


def _revision(row: Any) -> PlanRevision:
    return PlanRevision(
        str(row["id"]), str(row["plan_id"]), int(row["ordinal"]),
        str(row["content"]), str(row["sha256"]), str(row["source"]),
        str(row["created_by_run_id"]) if row["created_by_run_id"] else None,
        str(row["created_at"]),
    )


def _request(row: Any) -> PlanRequest:
    return PlanRequest(
        str(row["id"]), str(row["session_id"]),
        str(row["plan_id"]) if row["plan_id"] else None,
        str(row["revision_id"]) if row["revision_id"] else None,
        PlanRequestKind(str(row["kind"])), PlanRequestStatus(str(row["status"])),
        str(row["run_id"]) if row["run_id"] else None,
        str(row["invocation_id"]) if row["invocation_id"] else None,
        str(row["objective"]) if row["objective"] else None,
        str(row["choice"]) if row["choice"] else None,
        str(row["feedback"]) if row["feedback"] else None,
        str(row["created_at"]), str(row["decided_at"]) if row["decided_at"] else None,
    )


def _implementation(row: Any) -> PlanImplementation:
    return PlanImplementation(
        str(row["plan_id"]), str(row["revision_id"]), str(row["request_id"]),
        str(row["work_item_id"]), str(row["run_id"]) if row["run_id"] else None,
        str(row["status"]), str(row["created_at"]), str(row["updated_at"]),
    )


__all__ = ["PlanRepository"]
