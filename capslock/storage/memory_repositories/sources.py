"""Memory provenance and access persistence."""

from __future__ import annotations

from typing import Any

from ...domain import MemoryInfo
from .core import Repository, timestamp


class MemorySourceRepository(Repository):
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

    async def invalidate(self, memory_id: str, *, run_id: str | None = None) -> None:
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

    async def list(self, memory_id: str) -> list[dict[str, object]]:
        rows = await self.all(
            "SELECT * FROM memory_sources WHERE memory_id=? ORDER BY id", (memory_id,)
        )
        return [dict(row) for row in rows]

    async def add(self, memory_id: str, **source: Any) -> None:
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
