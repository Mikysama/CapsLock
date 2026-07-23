"""Run lookup, retry, completion, and usage queries."""

from __future__ import annotations

from ...domain import RunInfo
from .core import Repository
from .run_journal import RunJournalRepository
from .workflow_records import run


class RunRepository(Repository):
    def __init__(self, database, journal: RunJournalRepository) -> None:
        super().__init__(database)
        self.journal = journal

    async def get(
        self, run_id: str, *, session_id: str | None = None
    ) -> RunInfo | None:
        query, values = "SELECT * FROM runs WHERE id=?", [run_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = await self.one(query, tuple(values))
        return None if row is None else run(row)

    async def require(self, run_id: str, *, session_id: str | None = None) -> RunInfo:
        item = await self.get(run_id, session_id=session_id)
        if item is None:
            raise ValueError(f"run does not exist: {run_id}")
        return item

    async def retryable(self, session_id: str, prefix: str) -> RunInfo:
        rows = await self.all(
            """SELECT * FROM runs WHERE session_id=? AND substr(id,1,?)=?
               AND status IN ('failed','cancelled','interrupted','stopped') ORDER BY started_at DESC LIMIT 2""",
            (session_id, len(prefix), prefix),
        )
        if len(rows) > 1:
            raise ValueError("run id prefix is ambiguous")
        if not rows:
            raise ValueError("retryable run does not exist in this session")
        item = run(rows[0])
        if await self.journal.last_stable_step(item.id) is None:
            raise ValueError("run has no stable checkpoint")
        return item

    async def completed(self, run_id: str) -> bool:
        row = await self.one("SELECT status FROM runs WHERE id=?", (run_id,))
        return row is not None and str(row[0]) == "completed"

    async def session_cost(self, session_id: str) -> tuple[int, int, float]:
        row = await self.one(
            "SELECT coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),coalesce(sum(cost_usd),0) FROM runs WHERE session_id=?",
            (session_id,),
        )
        return (int(row[0]), int(row[1]), float(row[2])) if row else (0, 0, 0.0)
