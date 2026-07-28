"""Run lookup, retry, completion, and usage queries."""

from __future__ import annotations

from ...domain import RunInfo, RunKind
from .core import now
import uuid
from .core import Repository
from .journal.repository import RunJournalRepository
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
            """SELECT coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),coalesce(sum(cost_usd),0)
               FROM runs WHERE session_id=? AND kind IN ('agent','session_seed')""",
            (session_id,),
        )
        return (int(row[0]), int(row[1]), float(row[2])) if row else (0, 0, 0.0)

    async def create_hidden(
        self,
        session_id: str,
        *,
        kind: RunKind,
        question: str = "",
        status: str = "running",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        error_message: str | None = None,
    ) -> RunInfo:
        if kind is RunKind.AGENT:
            raise ValueError("agent runs must use the foreground workflow")
        timestamp = now()
        work_item_id, run_id = uuid.uuid4().hex, uuid.uuid4().hex
        work_status = "running" if status == "running" else status
        await self.execute(
            """INSERT INTO work_items(id,session_id,question,kind,status,position,error,created_at,updated_at)
               VALUES(?,?,?,?,?,0,?,?,?)""",
            (
                work_item_id,
                session_id,
                question,
                kind.value,
                work_status,
                error_message,
                timestamp,
                timestamp,
            ),
        )
        await self.execute(
            """INSERT INTO runs(id,session_id,work_item_id,question,kind,status,started_at,
               finished_at,duration_ms,input_tokens,output_tokens,cost_usd,error_message)
               VALUES(?,?,?,?,?,?,?,CASE WHEN ?='running' THEN NULL ELSE ? END,
               CASE WHEN ?='running' THEN NULL ELSE 0 END,?,?,?,?)""",
            (
                run_id,
                session_id,
                work_item_id,
                question,
                kind.value,
                status,
                timestamp,
                status,
                timestamp,
                status,
                input_tokens,
                output_tokens,
                cost_usd,
                error_message,
            ),
        )
        return await self.require(run_id)

    async def finish_hidden(
        self,
        run_id: str,
        *,
        status: str = "completed",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        error_message: str | None = None,
    ) -> RunInfo:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid hidden run status")
        timestamp = now()
        run = await self.require(run_id)
        await self.execute(
            """UPDATE runs SET status=?,finished_at=?,duration_ms=0,input_tokens=?,output_tokens=?,
               cost_usd=?,error_message=? WHERE id=? AND kind!='agent'""",
            (
                status,
                timestamp,
                input_tokens,
                output_tokens,
                cost_usd,
                error_message,
                run_id,
            ),
        )
        await self.execute(
            "UPDATE work_items SET status=?,error=?,updated_at=? WHERE id=?",
            (status, error_message, timestamp, run.work_item_id),
        )
        return await self.require(run_id)

    async def usage_breakdown(self, session_id: str | None = None) -> list[dict]:
        query = """SELECT kind,count(*) AS run_count,coalesce(sum(input_tokens),0) AS input_tokens,
                   coalesce(sum(output_tokens),0) AS output_tokens,coalesce(sum(cost_usd),0) AS cost_usd,
                   coalesce(sum(duration_ms),0) AS duration_ms FROM runs"""
        values: tuple[object, ...] = ()
        if session_id is not None:
            query += " WHERE session_id=?"
            values = (session_id,)
        query += " GROUP BY kind ORDER BY kind"
        return [dict(row) for row in await self.all(query, values)]
