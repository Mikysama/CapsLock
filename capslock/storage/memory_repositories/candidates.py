"""Memory extraction and candidate queue repository."""

from __future__ import annotations

import json
import uuid

from ...domain import (
    MemoryCandidateInfo,
    MemoryCandidateStatus,
    MemoryPolicy,
    MemoryScope,
    MemoryType,
)
from .core import Repository, timestamp


class CandidateRepository(Repository):
    async def start_extraction(
        self,
        *,
        workspace: str,
        session_id: str,
        source_run_id: str,
        model: str,
        prompt_version: str,
        policy: MemoryPolicy,
    ) -> str:
        identifier = uuid.uuid4().hex
        await self.execute(
            """INSERT INTO memory_extractions(id,workspace_key,session_id,source_run_id,model,prompt_version,policy,status,created_at)
               VALUES(?,?,?,?,?,?,?,'running',?)""",
            (
                identifier,
                workspace,
                session_id,
                source_run_id,
                model,
                prompt_version,
                policy.value,
                timestamp(),
            ),
        )
        return identifier

    async def finish_extraction(
        self,
        extraction_id: str,
        *,
        status: str,
        candidate_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_code: str | None = None,
    ) -> None:
        await self.execute(
            """UPDATE memory_extractions SET status=?,candidate_count=?,input_tokens=?,output_tokens=?,
               error_code=?,completed_at=? WHERE id=?""",
            (
                status,
                candidate_count,
                input_tokens,
                output_tokens,
                error_code,
                timestamp(),
                extraction_id,
            ),
        )

    async def create(
        self,
        *,
        extraction_id: str,
        content: str,
        memory_type: MemoryType,
        scope: MemoryScope,
        workspace: str,
        session_id: str,
        source_run_id: str,
        confidence: float,
        status: MemoryCandidateStatus = MemoryCandidateStatus.PENDING,
        relation: str = "new",
        related_memory_id: str | None = None,
        risk_flags: tuple[str, ...] = (),
    ) -> MemoryCandidateInfo:
        identifier = f"cand_{uuid.uuid4().hex}"
        await self.execute(
            """INSERT INTO memory_candidates(id,extraction_id,content,memory_type,scope,workspace_key,session_id,
               source_run_id,confidence,status,relation,related_memory_id,risk_flags_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                extraction_id,
                content,
                memory_type.value,
                scope.value,
                workspace,
                session_id,
                source_run_id,
                confidence,
                status.value,
                relation,
                related_memory_id,
                json.dumps(risk_flags),
                timestamp(),
            ),
        )
        return await self.require(identifier)

    async def get(self, candidate_id: str) -> MemoryCandidateInfo | None:
        row = await self.one(
            "SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)
        )
        return None if row is None else _candidate(row)

    async def require(self, candidate_id: str) -> MemoryCandidateInfo:
        item = await self.get(candidate_id)
        if item is None:
            raise ValueError("memory candidate does not exist")
        return item

    async def resolve(
        self, prefix: str, *, workspace: str, session_id: str
    ) -> MemoryCandidateInfo:
        rows = await self.all(
            """SELECT * FROM memory_candidates WHERE workspace_key=? AND session_id=?
               AND (id=? OR id LIKE ?) ORDER BY created_at LIMIT 2""",
            (workspace, session_id, prefix, f"{prefix}%"),
        )
        if len(rows) > 1:
            raise ValueError("candidate id prefix is ambiguous")
        if not rows:
            raise ValueError("memory candidate does not exist in this session")
        return _candidate(rows[0])

    async def list(
        self,
        *,
        workspace: str,
        session_id: str,
        include_all: bool = False,
        limit: int = 200,
    ) -> list[MemoryCandidateInfo]:
        query = "SELECT * FROM memory_candidates WHERE workspace_key=? AND session_id=?"
        values: list[object] = [workspace, session_id]
        if not include_all:
            query += " AND status IN ('pending','conflict')"
        query += " ORDER BY created_at LIMIT ?"
        values.append(limit)
        return [_candidate(row) for row in await self.all(query, tuple(values))]

    async def decide(
        self,
        candidate_id: str,
        status: MemoryCandidateStatus,
        *,
        adopted_memory_id: str | None = None,
        clear_content: bool = False,
    ) -> MemoryCandidateInfo:
        updated = await self.execute(
            """UPDATE memory_candidates SET status=?,adopted_memory_id=?,decided_at=?,
               content=CASE WHEN ? THEN NULL ELSE content END WHERE id=?""",
            (
                status.value,
                adopted_memory_id,
                timestamp(),
                int(clear_content),
                candidate_id,
            ),
        )
        if not updated:
            raise ValueError("memory candidate does not exist")
        return await self.require(candidate_id)

    async def purge(self, candidate_id: str) -> MemoryCandidateInfo:
        updated = await self.execute(
            "UPDATE memory_candidates SET content=NULL,status='purged',risk_flags_json='[]',decided_at=? WHERE id=?",
            (timestamp(), candidate_id),
        )
        if not updated:
            raise ValueError("memory candidate does not exist")
        return await self.require(candidate_id)

    async def cleanup(self, *, workspace: str, retention_days: int = 30) -> int:
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        return await self.execute(
            """UPDATE memory_candidates SET content=NULL,risk_flags_json='[]'
               WHERE workspace_key=? AND status IN ('rejected','duplicate')
               AND decided_at IS NOT NULL AND decided_at<=? AND content IS NOT NULL""",
            (workspace, cutoff),
        )


def _candidate(row) -> MemoryCandidateInfo:
    return MemoryCandidateInfo(
        id=str(row["id"]),
        extraction_id=str(row["extraction_id"]),
        content=row["content"],
        type=MemoryType(row["memory_type"]),
        scope=MemoryScope(row["scope"]),
        workspace_key=str(row["workspace_key"]),
        session_id=str(row["session_id"]),
        source_run_id=str(row["source_run_id"]),
        confidence=float(row["confidence"]),
        status=MemoryCandidateStatus(row["status"]),
        relation=str(row["relation"]),
        related_memory_id=row["related_memory_id"],
        risk_flags=tuple(json.loads(row["risk_flags_json"])),
        adopted_memory_id=row["adopted_memory_id"],
        created_at=str(row["created_at"]),
        decided_at=row["decided_at"],
    )
