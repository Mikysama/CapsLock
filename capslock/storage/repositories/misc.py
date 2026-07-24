"""Async task, source, settings, and export queries."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ...domain import SourceInfo, TaskInfo
from .core import Repository, now


class TaskRepository(Repository):
    async def create(
        self,
        session_id: str,
        *,
        subject: str,
        description: str = "",
        blocked_by: list[str] | None = None,
        owner: str | None = None,
        active_form: str | None = None,
        metadata: dict[str, object] | None = None,
        run_id: str | None = None,
    ) -> TaskInfo:
        normalized = subject.strip()
        if not normalized:
            raise ValueError("task subject must not be empty")
        identifier, timestamp = uuid.uuid4().hex, now()
        dependencies = list(dict.fromkeys(blocked_by or []))
        if identifier in dependencies:
            raise ValueError("a task cannot depend on itself")
        async with self.database.transaction() as connection:
            if dependencies:
                rows = await (
                    await connection.execute(
                        f"SELECT id FROM tasks WHERE session_id=? AND id IN ({','.join('?' for _ in dependencies)})",
                        (session_id, *dependencies),
                    )
                ).fetchall()
                if {str(row[0]) for row in rows} != set(dependencies):
                    raise ValueError("blocked_by contains a task outside this session")
            row = await (
                await connection.execute(
                    "SELECT coalesce(max(position),-1)+1 FROM tasks WHERE session_id=?",
                    (session_id,),
                )
            ).fetchone()
            await connection.execute(
                """INSERT INTO tasks(
                     id,session_id,run_id,subject,description,owner,active_form,
                     metadata_json,status,position,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    session_id,
                    run_id,
                    normalized,
                    description,
                    owner,
                    active_form,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    "pending",
                    int(row[0]),
                    timestamp,
                    timestamp,
                ),
            )
            await connection.executemany(
                "INSERT INTO task_dependencies(task_id,blocked_by_task_id,created_at) VALUES(?,?,?)",
                [(identifier, dependency, timestamp) for dependency in dependencies],
            )
        item = await self.get(identifier, session_id=session_id)
        assert item is not None
        return item

    async def replace(
        self, session_id: str, items: list[str], *, run_id: str | None = None
    ) -> list[TaskInfo]:
        timestamp = now()
        async with self.database.transaction() as connection:
            await connection.execute(
                "DELETE FROM tasks WHERE session_id=?", (session_id,)
            )
            await connection.executemany(
                """INSERT INTO tasks(id,session_id,run_id,subject,status,position,created_at,updated_at)
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
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        status: str | None = None,
    ) -> list[TaskInfo]:
        query, values = "SELECT * FROM tasks WHERE session_id=?", [session_id]
        if run_id is not None:
            query += " AND run_id=?"
            values.append(run_id)
        if status is not None:
            query += " AND status=?"
            values.append(status)
        query += " ORDER BY position,created_at"
        return [await self._task(row) for row in await self.all(query, tuple(values))]

    async def get(
        self, task_id: str, *, session_id: str | None = None
    ) -> TaskInfo | None:
        query, values = "SELECT * FROM tasks WHERE id=?", [task_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = await self.one(query, tuple(values))
        return None if row is None else await self._task(row)

    async def update(
        self,
        task_id: str,
        session_id: str,
        **changes: Any,
    ) -> TaskInfo:
        allowed = {
            "subject",
            "description",
            "owner",
            "active_form",
            "metadata",
            "status",
            "blocked_by",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)[0]}")
        current = await self.get(task_id, session_id=session_id)
        if current is None:
            raise ValueError("task does not belong to this session or does not exist")
        if "subject" in changes and not str(changes["subject"]).strip():
            raise ValueError("task subject must not be empty")
        if "status" in changes:
            self._validate_status(str(changes["status"]))
        assignments: list[str] = []
        values: list[object] = []
        for name in ("subject", "description", "owner", "active_form", "status"):
            if name in changes:
                assignments.append(f"{name}=?")
                values.append(changes[name])
        if "metadata" in changes:
            assignments.append("metadata_json=?")
            values.append(json.dumps(changes["metadata"] or {}, ensure_ascii=False))
        async with self.database.transaction() as connection:
            if assignments:
                assignments.append("updated_at=?")
                values.extend((now(), task_id, session_id))
                await connection.execute(
                    f"UPDATE tasks SET {','.join(assignments)} WHERE id=? AND session_id=?",
                    tuple(values),
                )
            if "blocked_by" in changes:
                dependencies = list(dict.fromkeys(changes["blocked_by"] or []))
                if task_id in dependencies:
                    raise ValueError("a task cannot depend on itself")
                await self._validate_dependencies(
                    connection, session_id, task_id, dependencies
                )
                await connection.execute(
                    "DELETE FROM task_dependencies WHERE task_id=?", (task_id,)
                )
                await connection.executemany(
                    "INSERT INTO task_dependencies(task_id,blocked_by_task_id,created_at) VALUES(?,?,?)",
                    [(task_id, dependency, now()) for dependency in dependencies],
                )
        item = await self.get(task_id, session_id=session_id)
        assert item is not None
        return item

    async def update_status(
        self, task_id: str, session_id: str, status: str
    ) -> TaskInfo:
        self._validate_status(status)
        return await self.update(task_id, session_id, status=status)

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in {
            "pending",
            "running",
            "blocked",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError(f"invalid task status: {status}")

    async def _validate_dependencies(
        self, connection, session_id: str, task_id: str, dependencies: list[str]
    ) -> None:
        for dependency in dependencies:
            row = await (
                await connection.execute(
                    "SELECT 1 FROM tasks WHERE id=? AND session_id=?",
                    (dependency, session_id),
                )
            ).fetchone()
            if row is None:
                raise ValueError("blocked_by contains a task outside this session")
        for dependency in dependencies:
            back = await (
                await connection.execute(
                    """WITH RECURSIVE reachable(id) AS (
                         SELECT blocked_by_task_id FROM task_dependencies WHERE task_id=?
                         UNION
                         SELECT d.blocked_by_task_id FROM task_dependencies d
                         JOIN reachable r ON d.task_id=r.id
                       ) SELECT 1 FROM reachable WHERE id=? LIMIT 1""",
                    (dependency, task_id),
                )
            ).fetchone()
            if back is not None or dependency == task_id:
                raise ValueError("task dependency cycle detected")

    async def _task(self, row) -> TaskInfo:
        dependencies = await self.all(
            "SELECT blocked_by_task_id FROM task_dependencies WHERE task_id=? ORDER BY blocked_by_task_id",
            (str(row["id"]),),
        )
        return TaskInfo(
            str(row["id"]),
            str(row["session_id"]),
            str(row["subject"]),
            str(row["status"]),
            row["run_id"],
            int(row["position"]),
            str(row["description"]),
            row["owner"],
            row["active_form"],
            json.loads(row["metadata_json"]),
            tuple(str(item["blocked_by_task_id"]) for item in dependencies),
        )


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
