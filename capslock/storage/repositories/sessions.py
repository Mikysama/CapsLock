"""Async session persistence."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ...domain import (
    SessionInfo,
    SessionTitleSource,
    normalize_session_title,
    pending_session_title,
)
from .core import Repository, now


class SessionRepository(Repository):
    def __init__(self, database, workspace: Path) -> None:
        super().__init__(database)
        self.workspace = workspace.resolve()

    async def create(self, model: str) -> SessionInfo:
        identifier, created = uuid.uuid4().hex, now()
        title = pending_session_title(created)
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO sessions(id,model,created_at,updated_at,title,title_source,title_updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    model,
                    created,
                    created,
                    title,
                    SessionTitleSource.PENDING.value,
                    created,
                ),
            )
            await connection.execute(
                "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
                (identifier, "title", title, created),
            )
        return await self.require(identifier)

    async def get(self, session_id: str) -> SessionInfo | None:
        row = await self.one("SELECT * FROM sessions WHERE id=?", (session_id,))
        return None if row is None else self._info(row)

    async def require(self, session_id: str) -> SessionInfo:
        item = await self.get(session_id)
        if item is None:
            raise ValueError(f"session does not exist: {session_id}")
        return item

    async def set_model(self, session_id: str, model: str) -> SessionInfo:
        updated = await self.execute(
            "UPDATE sessions SET model=?,updated_at=? WHERE id=?",
            (model, now(), session_id),
        )
        if not updated:
            raise ValueError(f"session does not exist: {session_id}")
        return await self.require(session_id)

    async def list(
        self, limit: int = 20, *, include_archived: bool = False
    ) -> list[SessionInfo]:
        query = "SELECT * FROM sessions WHERE deletion_state IS NULL"
        if not include_archived:
            query += " AND archived_at IS NULL"
        query += " ORDER BY updated_at DESC LIMIT ?"
        return [self._info(row) for row in await self.all(query, (limit,))]

    async def search(
        self, query: str, *, limit: int = 20, include_archived: bool = False
    ) -> list[SessionInfo]:
        normalized = query.strip()
        if not normalized:
            return await self.list(limit, include_archived=include_archived)
        sql = """SELECT s.*,min(bm25(session_search)) AS rank
                 FROM session_search JOIN sessions s ON s.id=session_search.session_id
                 WHERE session_search MATCH ? AND s.deletion_state IS NULL"""
        values: list[object] = [normalized]
        if not include_archived:
            sql += " AND s.archived_at IS NULL"
        sql += " GROUP BY s.id ORDER BY rank,s.updated_at DESC LIMIT ?"
        values.append(limit)
        try:
            rows = await self.all(sql, tuple(values))
        except Exception:
            escaped = normalized.replace("%", "\\%").replace("_", "\\_")
            sql = """SELECT DISTINCT s.* FROM sessions s LEFT JOIN messages m ON m.session_id=s.id
                     WHERE s.deletion_state IS NULL AND (s.title LIKE ? ESCAPE '\\' OR m.content LIKE ? ESCAPE '\\')"""
            fallback: list[object] = [f"%{escaped}%", f"%{escaped}%"]
            if not include_archived:
                sql += " AND s.archived_at IS NULL"
            sql += " ORDER BY s.updated_at DESC LIMIT ?"
            fallback.append(limit)
            rows = await self.all(sql, tuple(fallback))
        return [self._info(row) for row in rows]

    async def resolve(self, prefix: str) -> SessionInfo | None:
        normalized = prefix.strip()
        if not normalized:
            raise ValueError("session id cannot be empty")
        exact = await self.get(normalized)
        if exact is not None:
            return exact
        rows = await self.all(
            "SELECT * FROM sessions WHERE substr(id,1,?)=? ORDER BY updated_at DESC LIMIT 2",
            (len(normalized), normalized),
        )
        if len(rows) > 1:
            raise ValueError(f"session id prefix is ambiguous: {normalized}")
        return self._info(rows[0]) if rows else None

    async def rename(self, session_id: str, title: str) -> SessionInfo:
        normalized, timestamp = normalize_session_title(title), now()
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE sessions SET title=?,title_source=?,title_updated_at=?,updated_at=? WHERE id=?",
                (
                    normalized,
                    SessionTitleSource.MANUAL.value,
                    timestamp,
                    timestamp,
                    session_id,
                ),
            )
            if not cursor.rowcount:
                raise ValueError(f"session does not exist: {session_id}")
            await connection.execute(
                "DELETE FROM session_search WHERE session_id=? AND kind='title'",
                (session_id,),
            )
            await connection.execute(
                "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
                (session_id, "title", normalized, timestamp),
            )
        return await self.require(session_id)

    async def append_message(
        self, session_id: str, run_id: str, role: str, content: str
    ) -> None:
        timestamp = now()
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO messages(session_id,run_id,role,content,created_at) VALUES(?,?,?,?,?)",
                (session_id, run_id, role, content, timestamp),
            )
            await connection.execute(
                "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
                (session_id, "message", content, timestamp),
            )
            await connection.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?", (timestamp, session_id)
            )

    async def messages(
        self,
        session_id: str,
        limit: int = 24,
        *,
        excluded_run_ids: set[str] | None = None,
    ) -> list[dict[str, str]]:
        query = "SELECT role,content FROM messages WHERE session_id=?"
        values: list[object] = [session_id]
        if excluded_run_ids:
            query += (
                " AND run_id NOT IN (" + ",".join("?" for _ in excluded_run_ids) + ")"
            )
            values.extend(sorted(excluded_run_ids))
        query += " ORDER BY id DESC LIMIT ?"
        values.append(limit)
        rows = await self.all(query, tuple(values))
        return [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in reversed(rows)
        ]

    async def context_messages(
        self,
        session_id: str,
        limit: int = 24,
        *,
        excluded_run_ids: set[str] | None = None,
    ) -> list[dict[str, str]]:
        excluded = excluded_run_ids or set()
        entries = [
            {"role": str(entry["role"]), "content": str(entry["content"])}
            for entry in await self.transcript(session_id)
            if entry.get("run_id") not in excluded and str(entry.get("content", ""))
        ]
        return entries[-limit:]

    async def message_count(
        self, session_id: str, *, excluded_run_ids: set[str] | None = None
    ) -> int:
        query = "SELECT count(*) FROM messages WHERE session_id=?"
        values: list[object] = [session_id]
        if excluded_run_ids:
            query += (
                " AND run_id NOT IN (" + ",".join("?" for _ in excluded_run_ids) + ")"
            )
            values.extend(sorted(excluded_run_ids))
        row = await self.one(query, tuple(values))
        return int(row[0]) if row else 0

    async def compact_summary(
        self, session_id: str, keep: int, *, excluded_run_ids: set[str] | None = None
    ) -> str:
        query = "SELECT role,content FROM messages WHERE session_id=?"
        values: list[object] = [session_id]
        if excluded_run_ids:
            query += (
                " AND run_id NOT IN (" + ",".join("?" for _ in excluded_run_ids) + ")"
            )
            values.extend(sorted(excluded_run_ids))
        query += " ORDER BY id"
        rows = await self.all(query, tuple(values))
        older = rows[:-keep] if len(rows) > keep else []
        if not older:
            return ""
        summary = "\n".join(f"{row['role']}: {row['content']}" for row in older)[-6000:]
        await self.execute(
            "UPDATE sessions SET summary=?,updated_at=? WHERE id=?",
            (summary, now(), session_id),
        )
        return summary

    async def archive(self, session_id: str, *, archived: bool = True) -> SessionInfo:
        updated = await self.execute(
            "UPDATE sessions SET archived_at=?,updated_at=? WHERE id=? AND deletion_state IS NULL",
            (now() if archived else None, now(), session_id),
        )
        if not updated:
            raise ValueError(f"session does not exist: {session_id}")
        return await self.require(session_id)

    async def has_active_work(self, session_id: str) -> bool:
        row = await self.one(
            "SELECT 1 FROM work_items WHERE session_id=? AND status IN ('running','waiting_approval') LIMIT 1",
            (session_id,),
        )
        return row is not None

    async def delete_if_empty(self, session_id: str) -> bool:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """DELETE FROM sessions WHERE id=? AND title_source='pending'
                   AND NOT EXISTS(SELECT 1 FROM runs WHERE session_id=sessions.id)
                   AND NOT EXISTS(SELECT 1 FROM messages WHERE session_id=sessions.id)
                   AND NOT EXISTS(SELECT 1 FROM actions WHERE session_id=sessions.id)""",
                (session_id,),
            )
            if cursor.rowcount:
                await connection.execute(
                    "DELETE FROM session_search WHERE session_id=?", (session_id,)
                )
            return bool(cursor.rowcount)

    async def delete(self, session_id: str) -> None:
        if await self.has_active_work(session_id):
            raise ValueError(
                "active or approval-waiting sessions must be cancelled before deletion"
            )
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "DELETE FROM sessions WHERE id=?", (session_id,)
            )
            if not cursor.rowcount:
                raise ValueError(f"session does not exist: {session_id}")
            await connection.execute(
                "DELETE FROM session_search WHERE session_id=?", (session_id,)
            )

    async def transcript(self, session_id: str) -> list[dict[str, Any]]:
        entries: list[tuple[str, int, dict[str, Any]]] = []
        message_roles: dict[str, set[str]] = {}
        rows = await self.all(
            """SELECT m.id,m.role,m.content,m.run_id,m.created_at,r.status,r.error_message
               FROM messages m JOIN runs r ON r.id=m.run_id
               WHERE m.session_id=? ORDER BY m.id""",
            (session_id,),
        )
        for row in rows:
            run_id, role = str(row["run_id"]), str(row["role"])
            message_roles.setdefault(run_id, set()).add(role)
            entries.append(
                (
                    str(row["created_at"]),
                    int(row["id"]) * 2,
                    {
                        "role": role,
                        "content": str(row["content"]),
                        "run_id": run_id,
                        "status": str(row["status"]),
                        "error": row["error_message"],
                    },
                )
            )

        runs = await self.all(
            """SELECT id,question,status,started_at,error_message
               FROM runs WHERE session_id=? ORDER BY started_at""",
            (session_id,),
        )
        for index, row in enumerate(runs):
            run_id = str(row["id"])
            roles = message_roles.get(run_id, set())
            timestamp = str(row["started_at"])
            status = str(row["status"])
            if "user" not in roles:
                entries.append(
                    (
                        timestamp,
                        index * 2,
                        {
                            "role": "user",
                            "content": str(row["question"]),
                            "run_id": run_id,
                            "status": status,
                        },
                    )
                )
            if "assistant" not in roles and status != "completed":
                event_rows = await self.all(
                    """SELECT payload_json FROM run_events
                       WHERE run_id=? AND event_kind='text_delta' ORDER BY sequence""",
                    (run_id,),
                )
                text = "".join(
                    str(json.loads(event["payload_json"]).get("text", ""))
                    for event in event_rows
                )
                if text or row["error_message"]:
                    entries.append(
                        (
                            timestamp,
                            index * 2 + 1,
                            {
                                "role": "assistant",
                                "content": text,
                                "run_id": run_id,
                                "status": status,
                                "error": row["error_message"],
                            },
                        )
                    )
        entries.sort(key=lambda item: (item[0], item[1]))
        return [entry for _, _, entry in entries]

    def _info(self, row) -> SessionInfo:
        return SessionInfo(
            id=str(row["id"]),
            workspace=self.workspace,
            model=str(row["model"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            title=str(row["title"]),
            title_source=SessionTitleSource(row["title_source"]),
            title_updated_at=row["title_updated_at"],
            archived_at=row["archived_at"],
            deletion_state=row["deletion_state"],
        )
