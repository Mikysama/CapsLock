"""Memory extraction and candidate queue repository."""

from __future__ import annotations

import json
import uuid

from ...domain import (
    MemoryCandidateInfo,
    MemoryCandidateStatus,
    MemoryDurability,
    MemoryCandidateSourceInfo,
    MemoryPolicy,
    MemoryScope,
    MemoryType,
)
from .core import Repository, timestamp


_SELECT_CANDIDATE = """SELECT c.*,
 NULL AS source_message_id,NULL AS source_evidence_id,NULL AS source_quote,
 0 AS source_direct,0 AS source_verified FROM memory_candidates c"""


class CandidateRepository(Repository):
    async def extraction_segment(
        self, source_digest: str, model: str, prompt_version: str
    ) -> str | None:
        row = await self.one(
            """SELECT response_json FROM memory_extraction_segments
               WHERE source_digest=? AND model=? AND prompt_version=?""",
            (source_digest, model, prompt_version),
        )
        return None if row is None else str(row["response_json"])

    async def store_extraction_segment(
        self,
        *,
        source_digest: str,
        model: str,
        prompt_version: str,
        response_json: str,
        source_refs: list[str],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        await self.execute(
            """INSERT OR IGNORE INTO memory_extraction_segments(
               id,source_digest,model,prompt_version,response_json,source_refs_json,
               input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                f"segment_{uuid.uuid4().hex}",
                source_digest,
                model,
                prompt_version,
                response_json,
                json.dumps(source_refs, ensure_ascii=False),
                input_tokens,
                output_tokens,
                timestamp(),
            ),
        )

    async def start_extraction(
        self,
        *,
        workspace: str,
        session_id: str,
        source_run_id: str,
        model: str,
        prompt_version: str,
        policy: MemoryPolicy,
        envelope: dict[str, object] | None = None,
    ) -> str:
        identifier = uuid.uuid4().hex
        await self.execute(
            """INSERT INTO memory_extractions(id,workspace_key,session_id,source_run_id,envelope_json,model,prompt_version,policy,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,'running',?)""",
            (
                identifier,
                workspace,
                session_id,
                source_run_id,
                json.dumps(envelope or {}, ensure_ascii=False, sort_keys=True),
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
        namespace: str | None = None,
        subject: str | None = None,
        durability: MemoryDurability = MemoryDurability.DURABLE,
        why: str | None = None,
        how_to_apply: str | None = None,
        source: dict[str, object] | None = None,
        sources: tuple[dict[str, object], ...] = (),
        extractor_confidence: float | None = None,
        verifier_confidence: float | None = None,
        verification_status: str = "unverified",
        instruction_like: bool = False,
        calibration_version: str | None = None,
    ) -> MemoryCandidateInfo:
        identifier = f"cand_{uuid.uuid4().hex}"
        await self.execute(
            """INSERT INTO memory_candidates(id,extraction_id,content,memory_type,scope,namespace,
               subject,durability,why,how_to_apply,workspace_key,session_id,source_run_id,
               confidence,extractor_confidence,verifier_confidence,verification_status,
               instruction_like,calibration_version,status,relation,related_memory_id,
               risk_flags_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                extraction_id,
                content,
                memory_type.value,
                scope.value,
                namespace,
                subject,
                durability.value,
                why,
                how_to_apply,
                workspace,
                session_id,
                source_run_id,
                confidence,
                confidence if extractor_confidence is None else extractor_confidence,
                verifier_confidence,
                verification_status,
                int(instruction_like),
                calibration_version,
                status.value,
                relation,
                related_memory_id,
                json.dumps(risk_flags),
                timestamp(),
            ),
        )
        all_sources = sources or ((source,) if source is not None else ())
        for source_item in all_sources:
            await self.execute(
                """INSERT INTO memory_candidate_sources(
                   candidate_id,message_id,evidence_id,quote,direct,verified,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    identifier,
                    source_item.get("message_id"),
                    source_item.get("evidence_id"),
                    source_item["quote"],
                    int(bool(source_item.get("direct"))),
                    int(bool(source_item.get("verified"))),
                    timestamp(),
                ),
            )
        return await self.require(identifier)

    async def sources(self, candidate_id: str) -> tuple[MemoryCandidateSourceInfo, ...]:
        rows = await self.all(
            """SELECT message_id,evidence_id,quote,direct,verified
               FROM memory_candidate_sources WHERE candidate_id=? ORDER BY id""",
            (candidate_id,),
        )
        return tuple(
            MemoryCandidateSourceInfo(
                row["message_id"],
                row["evidence_id"],
                str(row["quote"]),
                bool(row["direct"]),
                bool(row["verified"]),
            )
            for row in rows
        )

    async def get(self, candidate_id: str) -> MemoryCandidateInfo | None:
        row = await self.one(_SELECT_CANDIDATE + " WHERE c.id=?", (candidate_id,))
        if row is None:
            return None
        return _with_sources(_candidate(row), await self.sources(candidate_id))

    async def require(self, candidate_id: str) -> MemoryCandidateInfo:
        item = await self.get(candidate_id)
        if item is None:
            raise ValueError("memory candidate does not exist")
        return item

    async def resolve(
        self, prefix: str, *, workspace: str, session_id: str
    ) -> MemoryCandidateInfo:
        rows = await self.all(
            _SELECT_CANDIDATE
            + """ WHERE c.workspace_key=? AND c.session_id=?
               AND (c.id=? OR c.id LIKE ?) ORDER BY c.created_at LIMIT 2""",
            (workspace, session_id, prefix, f"{prefix}%"),
        )
        if len(rows) > 1:
            raise ValueError("candidate id prefix is ambiguous")
        if not rows:
            raise ValueError("memory candidate does not exist in this session")
        item = _candidate(rows[0])
        return _with_sources(item, await self.sources(item.id))

    async def list(
        self,
        *,
        workspace: str,
        session_id: str,
        include_all: bool = False,
        limit: int = 200,
    ) -> list[MemoryCandidateInfo]:
        query = _SELECT_CANDIDATE + " WHERE c.workspace_key=? AND c.session_id=?"
        values: list[object] = [workspace, session_id]
        if not include_all:
            query += " AND c.status IN ('pending','conflict')"
        query += " ORDER BY c.created_at LIMIT ?"
        values.append(limit)
        candidates = [_candidate(row) for row in await self.all(query, tuple(values))]
        if not candidates:
            return []
        rows = []
        identifiers = [item.id for item in candidates]
        for start in range(0, len(identifiers), 900):
            batch = identifiers[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(
                await self.all(
                    f"""SELECT candidate_id,message_id,evidence_id,quote,direct,verified
                        FROM memory_candidate_sources
                        WHERE candidate_id IN ({placeholders})
                        ORDER BY candidate_id,id""",
                    tuple(batch),
                )
            )
        grouped: dict[str, list[MemoryCandidateSourceInfo]] = {}
        for row in rows:
            grouped.setdefault(str(row["candidate_id"]), []).append(
                MemoryCandidateSourceInfo(
                    row["message_id"],
                    row["evidence_id"],
                    str(row["quote"]),
                    bool(row["direct"]),
                    bool(row["verified"]),
                )
            )
        return [
            _with_sources(item, tuple(grouped.get(item.id, ()))) for item in candidates
        ]

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
    # Source metadata is projected by get/list callers below when available.
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
        namespace=row["namespace"],
        subject=row["subject"],
        durability=MemoryDurability(row["durability"] or "durable"),
        why=row["why"],
        how_to_apply=row["how_to_apply"],
        source_message_id=row["source_message_id"],
        source_evidence_id=row["source_evidence_id"],
        source_quote=row["source_quote"],
        direct=bool(row["source_direct"]),
        verified=bool(row["source_verified"]),
        extractor_confidence=float(row["extractor_confidence"] or 0),
        verifier_confidence=(
            float(row["verifier_confidence"])
            if row["verifier_confidence"] is not None
            else None
        ),
        verification_status=str(row["verification_status"]),
        instruction_like=bool(row["instruction_like"]),
        calibration_version=row["calibration_version"],
    )


def _with_sources(
    item: MemoryCandidateInfo,
    sources: tuple[MemoryCandidateSourceInfo, ...],
) -> MemoryCandidateInfo:
    from dataclasses import replace

    first = sources[0] if sources else None
    return replace(
        item,
        sources=sources,
        source_message_id=first.message_id if first else None,
        source_evidence_id=first.evidence_id if first else None,
        source_quote=first.quote if first else None,
        direct=first.direct if first else False,
        verified=first.verified if first else False,
    )
