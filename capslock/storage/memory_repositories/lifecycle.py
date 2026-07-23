"""Immutable-revision memory lifecycle persistence."""

from __future__ import annotations

import uuid

from ...domain import (
    MemoryInfo,
    MemoryOrigin,
    MemoryScope,
    MemoryStatus,
    MemoryType,
)
from .audit import record_memory_audit
from .core import Repository, timestamp
from .query import MemoryQueryRepository


_UNCHANGED = object()


class MemoryLifecycleRepository(Repository):
    def __init__(self, database, query: MemoryQueryRepository) -> None:
        super().__init__(database)
        self.query = query

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
            await record_memory_audit(
                connection, identifier, operation, scope, workspace, session_id, 1
            )
        return await self.query.require(identifier, include_inactive=True)

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
        current = await self.query.require(memory_id, include_inactive=True)
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
        current = await self.query.require(memory_id, include_inactive=True)
        if current.status is not MemoryStatus.ACTIVE:
            raise ValueError("only active memory can be forgotten")
        return await self._append_revision(
            current, operation="forget", status=MemoryStatus.FORGOTTEN
        )

    async def undo(self, memory_id: str) -> MemoryInfo:
        current = await self.query.require(memory_id, include_inactive=True)
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
            await record_memory_audit(
                connection,
                current.id,
                operation,
                current.scope,
                current.workspace_key,
                current.session_id,
                revision,
            )
        return await self.query.require(current.id, include_inactive=True)

    async def purge(self, memory_id: str) -> MemoryInfo:
        current = await self.query.require(memory_id, include_inactive=True)
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
            changed = timestamp()
            await connection.execute(
                "UPDATE memories SET status='purged',current_revision=NULL,purged_at=?,updated_at=? WHERE id=?",
                (changed, changed, memory_id),
            )
            await record_memory_audit(
                connection,
                memory_id,
                "purge",
                current.scope,
                current.workspace_key,
                current.session_id,
                current.revision,
            )
        return await self.query.require(memory_id, include_inactive=True)

    async def require(
        self, memory_id: str, *, include_inactive: bool = False
    ) -> MemoryInfo:
        return await self.query.require(memory_id, include_inactive=include_inactive)

    async def purge_session(self, *, workspace: str, session_id: str) -> int:
        rows = await self.all(
            "SELECT id FROM memories WHERE scope='session' AND workspace_key=? AND session_id=?",
            (workspace, session_id),
        )
        for row in rows:
            await self.purge(str(row[0]))
        return len(rows)
