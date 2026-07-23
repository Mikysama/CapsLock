"""Memory identity lookup, visibility, and lexical query persistence."""

from __future__ import annotations

from ...domain import MemoryInfo, MemoryScope
from .core import Repository, timestamp
from .records import (
    SELECT_MEMORY,
    escape_like,
    fts_query,
    memory_from_row,
    search_terms,
    visible_where,
)


class MemoryQueryRepository(Repository):
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
        return None if row is None else memory_from_row(row)

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
        where, values = visible_where(workspace, session_id)
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
        return memory_from_row(rows[0])

    async def list_visible(
        self,
        *,
        workspace: str,
        session_id: str,
        scope: MemoryScope | None = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> list[MemoryInfo]:
        where, values = visible_where(workspace, session_id)
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
        return [memory_from_row(row) for row in await self.all(query, tuple(values))]

    async def search_ranked(
        self, query: str, *, workspace: str, session_id: str, limit: int = 20
    ) -> list[tuple[MemoryInfo, int]]:
        where, values = visible_where(workspace, session_id)
        sql = (
            SELECT_MEMORY
            + f" JOIN memory_fts f ON f.memory_id=m.id AND f.revision=m.current_revision WHERE {where} AND memory_fts MATCH ? AND m.status='active' AND (r.expires_at IS NULL OR r.expires_at>?) ORDER BY bm25(memory_fts) LIMIT ?"
        )
        try:
            rows = await self.all(sql, (*values, fts_query(query), timestamp(), limit))
        except Exception:
            rows = []
        if not rows:
            terms = search_terms(query)
            clauses = " OR ".join("r.content LIKE ? ESCAPE '\\'" for _ in terms)
            sql = (
                SELECT_MEMORY
                + f" WHERE {where} AND ({clauses}) AND m.status='active' AND (r.expires_at IS NULL OR r.expires_at>?) ORDER BY m.updated_at DESC LIMIT ?"
            )
            rows = await self.all(
                sql,
                (
                    *values,
                    *[f"%{escape_like(term)}%" for term in terms],
                    timestamp(),
                    limit,
                ),
            )
        return [(memory_from_row(row), index) for index, row in enumerate(rows, 1)]

    async def search(
        self, query: str, *, workspace: str, session_id: str, limit: int = 10
    ) -> list[MemoryInfo]:
        return [
            item
            for item, _ in await self.search_ranked(
                query, workspace=workspace, session_id=session_id, limit=limit
            )
        ]
