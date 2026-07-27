"""Durable, idempotent memory background jobs."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from ...domain import MemoryJobStatus, MemoryJobType
from .core import Repository, timestamp


class MemoryJobRepository(Repository):
    async def recover(self) -> int:
        now = timestamp()
        terminal = await self.execute(
            """UPDATE memory_jobs SET status='failed',completed_at=?,
               error_code='process_interrupted' WHERE status='running' AND attempt_count>=3""",
            (now,),
        )
        queued = await self.execute(
            """UPDATE memory_jobs SET status='queued',started_at=NULL,
               available_at=?,error_code='process_interrupted'
               WHERE status='running' AND attempt_count<3""",
            (now,),
        )
        return terminal + queued

    async def enqueue(
        self,
        job_type: MemoryJobType,
        *,
        workspace: str,
        idempotency_key: str,
        payload: dict[str, object],
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        identifier = f"mjob_{uuid.uuid4().hex}"
        await self.execute(
            """INSERT OR IGNORE INTO memory_jobs(
               id,job_type,workspace_key,session_id,run_id,status,idempotency_key,
               payload_json,available_at,created_at)
               VALUES(?,?,?,?,?,'queued',?,?,?,?)""",
            (
                identifier,
                job_type.value,
                workspace,
                session_id,
                run_id,
                idempotency_key,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                timestamp(),
                timestamp(),
            ),
        )
        row = await self.one(
            "SELECT id FROM memory_jobs WHERE idempotency_key=?", (idempotency_key,)
        )
        assert row is not None
        return str(row[0])

    async def claim(
        self,
        *,
        workspace: str | None = None,
        job_type: MemoryJobType | None = None,
    ) -> dict[str, object] | None:
        now = timestamp()
        async with self.database.transaction() as connection:
            suffix, values = "", [now]
            if workspace is not None:
                suffix, values = " AND workspace_key=?", [now, workspace]
            if job_type is not None:
                suffix += " AND job_type=?"
                values.append(job_type.value)
            cursor = await connection.execute(
                f"""SELECT * FROM memory_jobs WHERE status='queued'
                    AND available_at<=?{suffix} ORDER BY created_at LIMIT 1""",
                values,
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
            if row is None:
                return None
            changed = await connection.execute(
                """UPDATE memory_jobs SET status='running',attempt_count=attempt_count+1,
                   started_at=?,error_code=NULL WHERE id=? AND status='queued'""",
                (now, row["id"]),
            )
            if not changed.rowcount:
                return None
        claimed = await self.one("SELECT * FROM memory_jobs WHERE id=?", (row["id"],))
        return _job(claimed) if claimed is not None else None

    async def complete(self, job_id: str) -> None:
        await self.execute(
            "UPDATE memory_jobs SET status='completed',completed_at=?,error_code=NULL WHERE id=?",
            (timestamp(), job_id),
        )

    async def fail(self, job_id: str, error_code: str) -> MemoryJobStatus:
        row = await self.one(
            "SELECT attempt_count FROM memory_jobs WHERE id=?", (job_id,)
        )
        if row is None:
            raise ValueError("memory job does not exist")
        attempts = int(row[0])
        if attempts >= 3:
            await self.execute(
                "UPDATE memory_jobs SET status='failed',completed_at=?,error_code=? WHERE id=?",
                (timestamp(), error_code, job_id),
            )
            return MemoryJobStatus.FAILED
        delay = 2 ** max(0, attempts - 1)
        available = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        await self.execute(
            """UPDATE memory_jobs SET status='queued',started_at=NULL,
               available_at=?,error_code=? WHERE id=?""",
            (available, error_code, job_id),
        )
        return MemoryJobStatus.QUEUED

    async def list(
        self, *, workspace: str, status: MemoryJobStatus | None = None, limit: int = 100
    ) -> list[dict[str, object]]:
        sql, values = "SELECT * FROM memory_jobs WHERE workspace_key=?", [workspace]
        if status is not None:
            sql += " AND status=?"
            values.append(status.value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        values.append(limit)
        return [_job(row) for row in await self.all(sql, tuple(values))]

    async def scrub_payload_for_memory(self, memory_id: str) -> int:
        """Remove purged text from queued/running payloads without deleting audit rows."""
        rows = await self.all(
            "SELECT id,payload_json FROM memory_jobs WHERE status IN ('queued','running')"
        )
        changed = 0
        for row in rows:
            payload = json.loads(row["payload_json"])
            encoded = json.dumps(payload, ensure_ascii=False)
            if memory_id not in encoded:
                continue
            payload = {"redacted": True, "purged_memory_id": memory_id}
            changed += await self.execute(
                "UPDATE memory_jobs SET payload_json=? WHERE id=?",
                (json.dumps(payload), row["id"]),
            )
        return changed


def _job(row) -> dict[str, object]:
    value = dict(row)
    value["job_type"] = MemoryJobType(value["job_type"])
    value["status"] = MemoryJobStatus(value["status"])
    value["payload"] = json.loads(value.pop("payload_json"))
    return value
