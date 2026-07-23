"""Embedding and recall persistence."""

from __future__ import annotations

import hashlib
import json

from ...domain import EmbeddingBackend, MemoryInfo, MemoryRecallHit
from .core import Repository, timestamp
from .records import MEMORY_COLUMNS, memory_from_row, visible_where


class EmbeddingRepository(Repository):
    async def put(
        self,
        item: MemoryInfo,
        *,
        backend: EmbeddingBackend,
        model: str,
        dimensions: int,
        vector: bytes,
        content_hash: str,
    ) -> None:
        await self.execute(
            """INSERT OR REPLACE INTO memory_embeddings(memory_id,revision,backend,model,dimensions,vector,content_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                item.id,
                item.revision,
                backend.value,
                model,
                dimensions,
                vector,
                content_hash,
                timestamp(),
            ),
        )

    async def list(
        self, *, workspace: str, session_id: str, backend: EmbeddingBackend, model: str
    ) -> list[tuple[MemoryInfo, bytes, int]]:
        where, values = visible_where(workspace, session_id)
        rows = await self.all(
            f"""SELECT {MEMORY_COLUMNS},e.vector,e.dimensions FROM memories m
                LEFT JOIN memory_revisions r ON r.memory_id=m.id AND r.revision=m.current_revision
                JOIN memory_embeddings e ON e.memory_id=m.id AND e.revision=m.current_revision
                WHERE e.backend=? AND e.model=? AND {where} AND m.status='active'
                AND (r.expires_at IS NULL OR r.expires_at>?)""",
            (backend.value, model, *values, timestamp()),
        )
        return [
            (memory_from_row(row), bytes(row["vector"]), int(row["dimensions"]))
            for row in rows
        ]

    async def clear(self, *, workspace: str | None = None) -> int:
        if workspace is None:
            return await self.execute("DELETE FROM memory_embeddings")
        return await self.execute(
            """DELETE FROM memory_embeddings WHERE memory_id IN
               (SELECT id FROM memories WHERE scope='global' OR workspace_key=?)""",
            (workspace,),
        )


class RecallRepository(Repository):
    async def record(
        self,
        *,
        workspace: str,
        session_id: str,
        run_id: str,
        query: str,
        hits: list[MemoryRecallHit],
    ) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT OR REPLACE INTO memory_recalls VALUES(?,?,?,?,?)",
                (
                    run_id,
                    workspace,
                    session_id,
                    hashlib.sha256(query.encode()).hexdigest(),
                    timestamp(),
                ),
            )
            await connection.execute(
                "DELETE FROM memory_recall_items WHERE run_id=?", (run_id,)
            )
            await connection.executemany(
                "INSERT INTO memory_recall_items VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        hit.memory.id,
                        hit.memory.revision,
                        hit.score,
                        hit.lexical_rank,
                        hit.semantic_rank,
                        json.dumps(hit.reasons, ensure_ascii=False),
                    )
                    for hit in hits
                ],
            )

    async def hits(
        self, *, workspace: str, session_id: str, run_id: str | None = None
    ) -> list[MemoryRecallHit]:
        if run_id is None:
            row = await self.one(
                "SELECT run_id FROM memory_recalls WHERE workspace_key=? AND session_id=? ORDER BY created_at DESC LIMIT 1",
                (workspace, session_id),
            )
            if row is None:
                return []
            run_id = str(row[0])
        rows = await self.all(
            f"""SELECT {MEMORY_COLUMNS},i.score,i.lexical_rank,i.semantic_rank,i.reasons_json
                FROM memories m LEFT JOIN memory_revisions r ON r.memory_id=m.id AND r.revision=m.current_revision
                JOIN memory_recall_items i ON i.memory_id=m.id AND i.revision=m.current_revision
                JOIN memory_recalls rr ON rr.run_id=i.run_id
                WHERE i.run_id=? AND rr.workspace_key=? AND rr.session_id=? ORDER BY i.score DESC""",
            (run_id, workspace, session_id),
        )
        return [
            MemoryRecallHit(
                memory_from_row(row),
                float(row["score"]),
                row["lexical_rank"],
                row["semantic_rank"],
                tuple(json.loads(row["reasons_json"])),
            )
            for row in rows
        ]
