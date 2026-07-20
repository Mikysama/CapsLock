"""Immutable-revision memory lifecycle and settings repository."""

from __future__ import annotations

import re
import uuid
from typing import Any

from ...domain import (
    EmbeddingBackend,
    MemoryInfo,
    MemoryOrigin,
    MemoryPolicy,
    MemoryScope,
    MemoryStatus,
    MemoryType,
)
from .core import Repository, timestamp


MEMORY_COLUMNS = """m.id,m.scope,m.workspace_key,m.session_id,m.status,m.current_revision,m.origin,
 m.source_valid,m.created_at AS memory_created_at,m.updated_at,m.purged_at,
 r.content,r.memory_type,r.source_kind,r.source_ref,r.confidence,r.expires_at"""
SELECT_MEMORY = f"""SELECT {MEMORY_COLUMNS}
 FROM memories m LEFT JOIN memory_revisions r ON r.memory_id=m.id AND r.revision=m.current_revision"""
_UNCHANGED = object()


class MemoryRepository(Repository):
    async def create(
        self,
        *,
        content: str,
        memory_type: MemoryType,
        scope: MemoryScope,
        workspace: str | None,
        session_id: str | None,
        source_kind: str,
        source_ref: str | None,
        confidence: float,
        expires_at: str | None,
        origin: MemoryOrigin,
        operation: str = "create",
        extraction_id: str | None = None,
        run_id: str | None = None,
    ) -> MemoryInfo:
        identifier, created = f"mem_{uuid.uuid4().hex}", timestamp()
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO memories(id,scope,workspace_key,session_id,status,current_revision,origin,created_at,updated_at)
                   VALUES(?,?,?,?, 'active',1,?,?,?)""",
                (
                    identifier,
                    scope.value,
                    workspace,
                    session_id,
                    origin.value,
                    created,
                    created,
                ),
            )
            await connection.execute(
                """INSERT INTO memory_revisions(memory_id,revision,operation,content,memory_type,source_kind,source_ref,confidence,expires_at,created_at)
                   VALUES(?,1,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    operation,
                    content,
                    memory_type.value,
                    source_kind,
                    source_ref,
                    confidence,
                    expires_at,
                    created,
                ),
            )
            await connection.execute(
                "INSERT INTO memory_fts(memory_id,revision,content) VALUES(?,1,?)",
                (identifier, content),
            )
            await connection.execute(
                """INSERT INTO memory_sources(memory_id,source_kind,source_ref,extraction_id,workspace_key,session_id,run_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    source_kind,
                    source_ref,
                    extraction_id,
                    workspace,
                    session_id,
                    run_id,
                    created,
                ),
            )
            await self._audit(
                connection, identifier, operation, scope, workspace, session_id, 1
            )
        return await self.require(identifier, include_inactive=True)

    async def edit(
        self,
        memory_id: str,
        *,
        content: str,
        memory_type: MemoryType,
        source_kind: str,
        source_ref: str | None,
        confidence: float,
        expires_at: str | None,
    ) -> MemoryInfo:
        current = await self.require(memory_id, include_inactive=True)
        if current.status is MemoryStatus.PURGED:
            raise ValueError("purged memory cannot be edited")
        return await self._append_revision(
            current,
            operation="edit",
            content=content,
            memory_type=memory_type,
            source_kind=source_kind,
            source_ref=source_ref,
            confidence=confidence,
            expires_at=expires_at,
            status=MemoryStatus.ACTIVE,
        )

    async def forget(self, memory_id: str) -> MemoryInfo:
        current = await self.require(memory_id, include_inactive=True)
        if current.status is not MemoryStatus.ACTIVE:
            raise ValueError("only active memory can be forgotten")
        return await self._append_revision(
            current, operation="forget", status=MemoryStatus.FORGOTTEN
        )

    async def undo(self, memory_id: str) -> MemoryInfo:
        current = await self.require(memory_id, include_inactive=True)
        if current.status is MemoryStatus.PURGED or current.revision <= 1:
            raise ValueError("memory has no reversible revision")
        previous = await self.one(
            "SELECT * FROM memory_revisions WHERE memory_id=? AND revision<? ORDER BY revision DESC LIMIT 1",
            (memory_id, current.revision),
        )
        assert previous is not None
        return await self._append_revision(
            current,
            operation="undo",
            content=str(previous["content"]),
            memory_type=MemoryType(previous["memory_type"]),
            source_kind=str(previous["source_kind"]),
            source_ref=previous["source_ref"],
            confidence=float(previous["confidence"]),
            expires_at=previous["expires_at"],
            status=MemoryStatus.ACTIVE,
        )

    async def _append_revision(
        self,
        current: MemoryInfo,
        *,
        operation: str,
        status: MemoryStatus,
        content: str | None = None,
        memory_type: MemoryType | None = None,
        source_kind: str | None = None,
        source_ref: str | None = None,
        confidence: float | None = None,
        expires_at: str | None | object = _UNCHANGED,
    ) -> MemoryInfo:
        revision, created = current.revision + 1, timestamp()
        value = content if content is not None else current.content or ""
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO memory_revisions(memory_id,revision,operation,content,memory_type,source_kind,source_ref,confidence,expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    current.id,
                    revision,
                    operation,
                    value,
                    (memory_type or current.type).value,
                    source_kind or current.source_kind,
                    current.source_ref if source_ref is None else source_ref,
                    current.confidence if confidence is None else confidence,
                    current.expires_at if expires_at is _UNCHANGED else expires_at,
                    created,
                ),
            )
            await connection.execute(
                "UPDATE memories SET current_revision=?,status=?,updated_at=? WHERE id=?",
                (revision, status.value, created, current.id),
            )
            await connection.execute(
                "DELETE FROM memory_fts WHERE memory_id=?", (current.id,)
            )
            if status is MemoryStatus.ACTIVE:
                await connection.execute(
                    "INSERT INTO memory_fts(memory_id,revision,content) VALUES(?,?,?)",
                    (current.id, revision, value),
                )
            await self._audit(
                connection,
                current.id,
                operation,
                current.scope,
                current.workspace_key,
                current.session_id,
                revision,
            )
        return await self.require(current.id, include_inactive=True)

    async def purge(self, memory_id: str) -> MemoryInfo:
        current = await self.require(memory_id, include_inactive=True)
        if current.status is MemoryStatus.PURGED:
            return current
        async with self.database.transaction() as connection:
            await connection.execute(
                "DELETE FROM memory_fts WHERE memory_id=?", (memory_id,)
            )
            await connection.execute(
                "DELETE FROM memory_sources WHERE memory_id=?", (memory_id,)
            )
            await connection.execute(
                "DELETE FROM memory_revisions WHERE memory_id=?", (memory_id,)
            )
            await connection.execute(
                "UPDATE memories SET status='purged',current_revision=NULL,purged_at=?,updated_at=? WHERE id=?",
                (timestamp(), timestamp(), memory_id),
            )
            await self._audit(
                connection,
                memory_id,
                "purge",
                current.scope,
                current.workspace_key,
                current.session_id,
                current.revision,
            )
        return await self.require(memory_id, include_inactive=True)

    async def get(
        self, memory_id: str, *, include_inactive: bool = False
    ) -> MemoryInfo | None:
        query = SELECT_MEMORY + " WHERE m.id=?"
        if not include_inactive:
            query += (
                " AND m.status='active' AND (r.expires_at IS NULL OR r.expires_at>?)"
            )
            row = await self.one(query, (memory_id, timestamp()))
        else:
            row = await self.one(query, (memory_id,))
        return None if row is None else _memory(row)

    async def require(
        self, memory_id: str, *, include_inactive: bool = False
    ) -> MemoryInfo:
        item = await self.get(memory_id, include_inactive=include_inactive)
        if item is None:
            raise ValueError("memory does not exist")
        return item

    async def resolve(
        self,
        prefix: str,
        *,
        workspace: str,
        session_id: str,
        include_inactive: bool = True,
    ) -> MemoryInfo:
        where, values = _visible_where(workspace, session_id)
        query = SELECT_MEMORY + f" WHERE {where} AND (m.id=? OR m.id LIKE ?)"
        values.extend((prefix, f"{prefix}%"))
        if not include_inactive:
            query += (
                " AND m.status='active' AND (r.expires_at IS NULL OR r.expires_at>?)"
            )
            values.append(timestamp())
        rows = await self.all(query + " ORDER BY m.created_at LIMIT 2", tuple(values))
        if len(rows) > 1:
            raise ValueError("memory id prefix is ambiguous")
        if not rows:
            raise ValueError("memory does not exist in the visible scope")
        return _memory(rows[0])

    async def list_visible(
        self,
        *,
        workspace: str,
        session_id: str,
        scope: MemoryScope | None = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> list[MemoryInfo]:
        where, values = _visible_where(workspace, session_id)
        query = SELECT_MEMORY + f" WHERE {where}"
        if scope is not None:
            query += " AND m.scope=?"
            values.append(scope.value)
        if not include_inactive:
            query += (
                " AND m.status='active' AND (r.expires_at IS NULL OR r.expires_at>?)"
            )
            values.append(timestamp())
        query += " ORDER BY m.updated_at DESC LIMIT ?"
        values.append(limit)
        return [_memory(row) for row in await self.all(query, tuple(values))]

    async def search_ranked(
        self, query: str, *, workspace: str, session_id: str, limit: int = 20
    ) -> list[tuple[MemoryInfo, int]]:
        where, values = _visible_where(workspace, session_id)
        sql = (
            SELECT_MEMORY
            + f" JOIN memory_fts f ON f.memory_id=m.id AND f.revision=m.current_revision WHERE {where} AND memory_fts MATCH ? AND m.status='active' AND (r.expires_at IS NULL OR r.expires_at>?) ORDER BY bm25(memory_fts) LIMIT ?"
        )
        try:
            rows = await self.all(sql, (*values, _fts_query(query), timestamp(), limit))
        except Exception:
            rows = []
        if not rows:
            terms = _search_terms(query)
            clauses = " OR ".join("r.content LIKE ? ESCAPE '\\'" for _ in terms)
            sql = (
                SELECT_MEMORY
                + f" WHERE {where} AND ({clauses}) AND m.status='active' AND (r.expires_at IS NULL OR r.expires_at>?) ORDER BY m.updated_at DESC LIMIT ?"
            )
            rows = await self.all(
                sql,
                (
                    *values,
                    *[f"%{_escape_like(term)}%" for term in terms],
                    timestamp(),
                    limit,
                ),
            )
        return [(_memory(row), index) for index, row in enumerate(rows, start=1)]

    async def search(
        self, query: str, *, workspace: str, session_id: str, limit: int = 10
    ) -> list[MemoryInfo]:
        return [
            item
            for item, _ in await self.search_ranked(
                query, workspace=workspace, session_id=session_id, limit=limit
            )
        ]

    async def record_access(
        self,
        memories: list[MemoryInfo],
        *,
        workspace: str,
        session_id: str,
        run_id: str,
    ) -> None:
        async with self.database.transaction() as connection:
            await connection.executemany(
                "INSERT OR IGNORE INTO memory_accesses VALUES(?,?,?,?,?,?)",
                [
                    (item.id, item.revision, workspace, session_id, run_id, timestamp())
                    for item in memories
                ],
            )

    async def excluded_runs(self, *, workspace: str, session_id: str) -> set[str]:
        rows = await self.all(
            """SELECT DISTINCT a.run_id FROM memory_accesses a JOIN memories m ON m.id=a.memory_id
               WHERE a.workspace_key=? AND a.session_id=? AND m.status!='active'""",
            (workspace, session_id),
        )
        return {str(row[0]) for row in rows}

    async def invalidate_source(
        self, memory_id: str, *, run_id: str | None = None
    ) -> None:
        async with self.database.transaction() as connection:
            suffix, values = "", [timestamp(), memory_id]
            if run_id is not None:
                suffix, values = " AND run_id=?", [timestamp(), memory_id, run_id]
            await connection.execute(
                f"UPDATE memory_sources SET valid=0,invalidated_at=? WHERE memory_id=?{suffix}",
                values,
            )
            valid = await (
                await connection.execute(
                    "SELECT 1 FROM memory_sources WHERE memory_id=? AND valid=1",
                    (memory_id,),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE memories SET source_valid=? WHERE id=?",
                (int(valid is not None), memory_id),
            )

    async def sources(self, memory_id: str) -> list[dict[str, object]]:
        rows = await self.all(
            "SELECT * FROM memory_sources WHERE memory_id=? ORDER BY id", (memory_id,)
        )
        return [dict(row) for row in rows]

    async def add_source(self, memory_id: str, **source: Any) -> None:
        await self.execute(
            """INSERT OR IGNORE INTO memory_sources(memory_id,source_kind,source_ref,extraction_id,workspace_key,session_id,run_id,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                memory_id,
                source["source_kind"],
                source.get("source_ref"),
                source.get("extraction_id"),
                source.get("workspace"),
                source.get("session_id"),
                source.get("run_id"),
                timestamp(),
            ),
        )
        await self.execute(
            "UPDATE memories SET source_valid=1 WHERE id=?", (memory_id,)
        )

    async def purge_session(self, *, workspace: str, session_id: str) -> int:
        rows = await self.all(
            "SELECT id FROM memories WHERE scope='session' AND workspace_key=? AND session_id=?",
            (workspace, session_id),
        )
        for row in rows:
            await self.purge(str(row[0]))
        return len(rows)

    async def settings(self, workspace: str) -> dict[str, object]:
        await self.execute(
            "INSERT OR IGNORE INTO memory_workspace_settings(workspace_key) VALUES(?)",
            (workspace,),
        )
        row = await self.one(
            "SELECT * FROM memory_workspace_settings WHERE workspace_key=?",
            (workspace,),
        )
        assert row is not None
        return {
            "write_enabled": bool(row["write_enabled"]),
            "policy": MemoryPolicy(row["policy"]),
            "recall_enabled": bool(row["recall_enabled"]),
            "embedding_backend": EmbeddingBackend(row["embedding_backend"]),
            "embedding_model": row["embedding_model"],
            "embedding_endpoint": row["embedding_endpoint"],
            "embedding_provider": row["embedding_provider"],
            "embedding_data_policy": row["embedding_data_policy"],
            "embedding_consent_id": row["embedding_consent_id"],
        }

    async def set_setting(self, workspace: str, name: str, value: object) -> None:
        if name not in {
            "write_enabled",
            "policy",
            "recall_enabled",
            "embedding_backend",
            "embedding_model",
            "embedding_endpoint",
            "embedding_provider",
            "embedding_data_policy",
            "embedding_consent_id",
        }:
            raise ValueError("unsupported memory setting")
        await self.execute(
            "INSERT OR IGNORE INTO memory_workspace_settings(workspace_key) VALUES(?)",
            (workspace,),
        )
        await self.execute(
            f"UPDATE memory_workspace_settings SET {name}=? WHERE workspace_key=?",
            (value, workspace),
        )

    async def audit_export(
        self, *, workspace: str, session_id: str, scope: MemoryScope, count: int
    ) -> None:
        await self.execute(
            "INSERT INTO memory_audit(operation,scope,workspace_key,session_id,detail,created_at) VALUES('export',?,?,?,?,?)",
            (scope.value, workspace, session_id, f"count={count}", timestamp()),
        )

    @staticmethod
    async def _audit(
        connection,
        memory_id: str,
        operation: str,
        scope: MemoryScope,
        workspace: str | None,
        session_id: str | None,
        revision: int,
    ) -> None:
        await connection.execute(
            "INSERT INTO memory_audit(memory_id,operation,scope,workspace_key,session_id,revision,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                memory_id,
                operation,
                scope.value,
                workspace,
                session_id,
                revision,
                timestamp(),
            ),
        )


def _visible_where(workspace: str, session_id: str) -> tuple[str, list[Any]]:
    return (
        "(m.scope='global' OR (m.scope='workspace' AND m.workspace_key=?) OR (m.scope='session' AND m.workspace_key=? AND m.session_id=?))",
        [workspace, workspace, session_id],
    )


def _search_terms(query: str) -> list[str]:
    terms = [item.casefold() for item in re.findall(r"[A-Za-z0-9_]+", query)]
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(item for item in terms if len(item) >= 2))[:32] or [
        query.strip()
    ]


def _fts_query(query: str) -> str:
    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in _search_terms(query)
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _memory(row) -> MemoryInfo:
    revision = int(row["current_revision"] or 0)
    return MemoryInfo(
        id=str(row["id"]),
        content=row["content"],
        type=MemoryType(row["memory_type"] or MemoryType.NOTE.value),
        scope=MemoryScope(row["scope"]),
        workspace_key=row["workspace_key"],
        session_id=row["session_id"],
        source_kind=str(row["source_kind"] or "purged"),
        source_ref=row["source_ref"],
        confidence=float(row["confidence"] or 0),
        expires_at=row["expires_at"],
        revision=revision,
        status=MemoryStatus(row["status"]),
        created_at=str(row["memory_created_at"]),
        updated_at=str(row["updated_at"]),
        purged_at=row["purged_at"],
        origin=MemoryOrigin(row["origin"]),
        source_valid=bool(row["source_valid"]),
    )
