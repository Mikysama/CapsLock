"""Foreground work queue persistence."""

from __future__ import annotations

import hashlib
import json
import uuid

from ...domain import (
    RunKind,
    WorkItemInfo,
    WorkItemStatus,
    validate_work_item_transition,
)
from .core import Repository, now
from .workflow_records import work_item


class WorkItemRepository(Repository):
    async def enqueue(
        self,
        session_id: str,
        question: str,
        *,
        parent_work_item_id: str | None = None,
        kind: RunKind = RunKind.AGENT,
    ) -> WorkItemInfo:
        identifier = uuid.uuid4().hex
        async with self.database.transaction() as connection:
            await self._insert_queued(
                connection, identifier, session_id, question, kind, parent_work_item_id
            )
        return await self.require(identifier)

    async def reserve_request(
        self, session_id: str, request_id: str, question: str
    ) -> tuple[WorkItemInfo, bool]:
        """Atomically deduplicate local client requests using the existing durable keys."""
        key = (
            "app_server.request."
            + hashlib.sha256(f"{session_id}:{request_id}".encode()).hexdigest()
        )
        digest = hashlib.sha256(question.encode()).hexdigest()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT value FROM workspace_settings WHERE key=?", (key,)
                )
            ).fetchone()
            created = row is None
            if row is not None:
                previous = json.loads(row[0])
                if previous["question_sha256"] != digest:
                    raise ValueError(
                        "request_id already belongs to a different question"
                    )
                identifier = previous["work_item_id"]
            else:
                identifier = uuid.uuid4().hex
                await self._insert_queued(
                    connection, identifier, session_id, question, RunKind.AGENT, None
                )
                await connection.execute(
                    "INSERT INTO workspace_settings(key,value) VALUES(?,?)",
                    (
                        key,
                        json.dumps(
                            {"work_item_id": identifier, "question_sha256": digest}
                        ),
                    ),
                )
        return await self.require(identifier), created

    @staticmethod
    async def _insert_queued(
        connection, identifier, session_id, question, kind, parent
    ):
        timestamp = now()
        row = await (
            await connection.execute(
                "SELECT coalesce(max(position),-1)+1 FROM work_items WHERE session_id=? AND status='queued'",
                (session_id,),
            )
        ).fetchone()
        await connection.execute(
            """INSERT INTO work_items(id,session_id,question,kind,status,position,parent_work_item_id,created_at,updated_at)
               VALUES(?,?,?,?,'queued',?,?,?,?)""",
            (
                identifier,
                session_id,
                question,
                kind.value,
                int(row[0]),
                parent,
                timestamp,
                timestamp,
            ),
        )

    async def get(self, item_id: str) -> WorkItemInfo | None:
        row = await self.one(
            """SELECT w.*,(SELECT r.id FROM runs r WHERE r.work_item_id=w.id ORDER BY r.started_at DESC LIMIT 1) current_run_id FROM work_items w WHERE w.id=?""",
            (item_id,),
        )
        return None if row is None else work_item(row)

    async def require(self, item_id: str) -> WorkItemInfo:
        item = await self.get(item_id)
        if item is None:
            raise ValueError(f"work item does not exist: {item_id}")
        return item

    async def list(
        self, session_id: str, *, active_only: bool = False
    ) -> list[WorkItemInfo]:
        query = """SELECT w.*,(SELECT r.id FROM runs r WHERE r.work_item_id=w.id ORDER BY r.started_at DESC LIMIT 1) current_run_id FROM work_items w WHERE w.session_id=?"""
        if active_only:
            query += (
                " AND w.status IN "
                "('queued','running','waiting_approval','waiting_input')"
            )
        query += " ORDER BY w.position,w.created_at"
        return [work_item(row) for row in await self.all(query, (session_id,))]

    async def update(
        self, item_id: str, status: WorkItemStatus, *, error: str | None = None
    ) -> WorkItemInfo:
        current = await self.require(item_id)
        validate_work_item_transition(current.status, status)
        updated = await self.execute(
            "UPDATE work_items SET status=?,error=?,updated_at=? WHERE id=? AND status=?",
            (status.value, error, now(), item_id, current.status.value),
        )
        if not updated:
            raise ValueError("work item changed concurrently")
        return await self.require(item_id)

    async def reorder(self, item_id: str, position: int) -> WorkItemInfo:
        item = await self.require(item_id)
        if item.status is not WorkItemStatus.QUEUED:
            raise ValueError("only queued work items can be reordered")
        await self.execute(
            "UPDATE work_items SET position=?,updated_at=? WHERE id=?",
            (max(0, position), now(), item_id),
        )
        return await self.require(item_id)
