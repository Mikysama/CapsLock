"""Secret-safe local performance span persistence."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ...security import redact
from .core import Repository, now


class PerformanceRepository(Repository):
    async def record(
        self,
        *,
        trace_id: str,
        category: str,
        name: str,
        duration_ms: float,
        status: str = "ok",
        session_id: str | None = None,
        run_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        identifier = f"span_{uuid.uuid4().hex}"
        safe = redact(attributes or {})
        # Spans intentionally accept scalar operational metadata only.
        safe = {
            str(key): value
            for key, value in safe.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        await self.execute(
            """INSERT INTO performance_spans(id,trace_id,session_id,run_id,parent_span_id,
               category,name,status,duration_ms,attributes_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                trace_id,
                session_id,
                run_id,
                parent_span_id,
                category,
                name,
                status,
                max(0.0, duration_ms),
                json.dumps(safe, sort_keys=True, ensure_ascii=False),
                now(),
            ),
        )
        return identifier

    async def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.all(
            "SELECT * FROM performance_spans ORDER BY created_at DESC LIMIT ?",
            (min(max(limit, 1), 1000),),
        )
        return [_decode(dict(row)) for row in rows]

    async def trace(self, trace_id: str) -> list[dict[str, Any]]:
        rows = await self.all(
            "SELECT * FROM performance_spans WHERE trace_id=? ORDER BY created_at,id",
            (trace_id,),
        )
        return [_decode(dict(row)) for row in rows]

    async def summary(self) -> list[dict[str, Any]]:
        rows = await self.all(
            """SELECT category,name,count(*) samples,avg(duration_ms) average_ms,
               max(duration_ms) maximum_ms FROM performance_spans
               GROUP BY category,name ORDER BY average_ms DESC"""
        )
        return [dict(row) for row in rows]

    async def prune(self, *, days: int = 30, maximum: int = 100_000) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, days))).isoformat()
        expired = await self.execute(
            "DELETE FROM performance_spans WHERE created_at<?", (cutoff,)
        )
        overflow = await self.execute(
            """DELETE FROM performance_spans WHERE id IN (
               SELECT id FROM performance_spans ORDER BY created_at DESC LIMIT -1 OFFSET ?
               )""",
            (max(1, maximum),),
        )
        return expired + overflow


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    row["attributes"] = json.loads(row.pop("attributes_json"))
    return row


__all__ = ["PerformanceRepository"]
