"""Async task, source, settings, and export queries."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ...domain import SourceInfo, TaskInfo
from .core import Repository, now


class TaskRepository(Repository):
    async def replace(
        self, session_id: str, items: list[str], *, run_id: str | None = None
    ) -> list[TaskInfo]:
        timestamp = now()
        async with self.database.transaction() as connection:
            await connection.execute(
                "DELETE FROM tasks WHERE session_id=?", (session_id,)
            )
            await connection.executemany(
                """INSERT INTO tasks(id,session_id,run_id,text,status,position,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (
                        uuid.uuid4().hex,
                        session_id,
                        run_id,
                        text,
                        "pending",
                        index,
                        timestamp,
                        timestamp,
                    )
                    for index, text in enumerate(items)
                ],
            )
        return await self.list(session_id)

    async def list(
        self, session_id: str, *, run_id: str | None = None
    ) -> list[TaskInfo]:
        query, values = "SELECT * FROM tasks WHERE session_id=?", [session_id]
        if run_id is not None:
            query += " AND run_id=?"
            values.append(run_id)
        query += " ORDER BY position,created_at"
        return [_task(row) for row in await self.all(query, tuple(values))]

    async def update_status(
        self, task_id: str, session_id: str, status: str
    ) -> TaskInfo:
        if status not in {
            "pending",
            "running",
            "blocked",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError(f"invalid task status: {status}")
        updated = await self.execute(
            "UPDATE tasks SET status=?,updated_at=? WHERE id=? AND session_id=?",
            (status, now(), task_id, session_id),
        )
        if not updated:
            raise ValueError("task does not belong to this session or does not exist")
        row = await self.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        assert row is not None
        return _task(row)


class SourceRepository(Repository):
    async def add(
        self,
        *,
        session_id: str,
        run_id: str,
        url: str,
        title: str,
        excerpt: str,
        suspicious: bool = False,
    ) -> SourceInfo:
        identifier, timestamp = uuid.uuid4().hex, now()
        await self.execute(
            "INSERT INTO sources(id,session_id,run_id,url,title,excerpt,fetched_at,suspicious) VALUES(?,?,?,?,?,?,?,?)",
            (
                identifier,
                session_id,
                run_id,
                url,
                title,
                excerpt,
                timestamp,
                int(suspicious),
            ),
        )
        item = await self.get(identifier, session_id=session_id)
        assert item is not None
        return item

    async def get(
        self, source_id: str, *, session_id: str | None = None
    ) -> SourceInfo | None:
        query, values = "SELECT * FROM sources WHERE id=?", [source_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = await self.one(query, tuple(values))
        return None if row is None else _source(row)

    async def list(self, session_id: str) -> list[SourceInfo]:
        return [
            _source(row)
            for row in await self.all(
                "SELECT * FROM sources WHERE session_id=? ORDER BY fetched_at",
                (session_id,),
            )
        ]


class SettingsRepository(Repository):
    async def workspace(self, key: str, default: str | None = None) -> str | None:
        row = await self.one("SELECT value FROM workspace_settings WHERE key=?", (key,))
        return default if row is None else str(row[0])

    async def set_workspace(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO workspace_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def skill_enabled(self, name: str) -> bool:
        row = await self.one("SELECT enabled FROM skill_settings WHERE name=?", (name,))
        return row is None or bool(row[0])

    async def set_skill_enabled(self, name: str, enabled: bool) -> None:
        await self.execute(
            """INSERT INTO skill_settings(name,enabled,updated_at) VALUES(?,?,?)
               ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
            (name, int(enabled), now()),
        )

    async def disabled_skills(self) -> set[str]:
        rows = await self.all("SELECT name FROM skill_settings WHERE enabled=0")
        return {str(row[0]) for row in rows}


class SnapshotRepository(Repository):
    TABLES = (
        "sessions",
        "messages",
        "work_items",
        "runs",
        "run_steps",
        "run_events",
        "actions",
        "tasks",
        "sources",
        "tool_calls",
        "citations",
        "run_governance",
        "tool_call_attempts",
    )

    async def session(self, session_id: str) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for table in self.TABLES:
            if table == "sessions":
                query = "SELECT * FROM sessions WHERE id=?"
            elif table in {
                "run_steps",
                "run_events",
                "tool_calls",
                "citations",
                "run_governance",
                "tool_call_attempts",
            }:
                query = f"SELECT t.* FROM {table} t JOIN runs r ON r.id=t.run_id WHERE r.session_id=? ORDER BY t.rowid"
            else:
                query = f"SELECT * FROM {table} WHERE session_id=? ORDER BY rowid"
            rows = await self.all(query, (session_id,))
            document[table] = [_decode(dict(row)) for row in rows]
        return document


def _decode(record: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "request_json",
        "result_json",
        "payload_json",
        "checkpoint_json",
        "arguments_json",
        "limits_json",
        "history_json",
    ):
        if record.get(key):
            record[key.removesuffix("_json")] = json.loads(record.pop(key))
    return record


def _task(row) -> TaskInfo:
    return TaskInfo(
        str(row["id"]),
        str(row["session_id"]),
        str(row["text"]),
        str(row["status"]),
        row["run_id"],
        int(row["position"]),
    )


def _source(row) -> SourceInfo:
    return SourceInfo(
        str(row["id"]),
        str(row["session_id"]),
        str(row["run_id"]),
        str(row["url"]),
        str(row["title"]),
        str(row["excerpt"]),
        str(row["fetched_at"]),
        bool(row["suspicious"]),
    )
