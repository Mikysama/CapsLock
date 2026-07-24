"""Immutable context compaction persistence."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .core import Repository, now


@dataclass(frozen=True)
class CompactionRecord:
    id: str
    session_id: str
    summary: dict[str, object]
    source_digest: str
    first_message_id: int | None
    last_message_id: int | None
    source_tokens: int
    target_tokens: int
    model_profile: str
    input_tokens: int = 0
    output_tokens: int = 0
    focus_instructions: str | None = None
    created_at: str = ""


class ContextCompactionRepository(Repository):
    async def matching(
        self, session_id: str, source_digest: str
    ) -> CompactionRecord | None:
        row = await self.one(
            """SELECT * FROM context_compactions
               WHERE session_id=? AND source_digest=? AND valid=1
               ORDER BY created_at DESC LIMIT 1""",
            (session_id, source_digest),
        )
        return None if row is None else _record(row)

    async def latest(self, session_id: str) -> CompactionRecord | None:
        row = await self.one(
            """SELECT * FROM context_compactions
               WHERE session_id=? AND valid=1 ORDER BY created_at DESC LIMIT 1""",
            (session_id,),
        )
        return None if row is None else _record(row)

    async def active(self, session_id: str) -> CompactionRecord | None:
        row = await self.one(
            """SELECT c.* FROM session_context_state s
               JOIN context_compactions c ON c.id=s.active_compaction_id
               WHERE s.session_id=? AND c.valid=1""",
            (session_id,),
        )
        return None if row is None else _record(row)

    async def activate(self, session_id: str, compaction_id: str) -> CompactionRecord:
        record = await self.one(
            """SELECT * FROM context_compactions WHERE id=? AND session_id=?
               AND first_message_id IS NOT NULL AND last_message_id IS NOT NULL AND valid=1""",
            (compaction_id, session_id),
        )
        if record is None:
            raise ValueError("only a valid session-history compaction can be activated")
        await self.execute(
            """INSERT INTO session_context_state(session_id,active_compaction_id,updated_at)
               VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET
               active_compaction_id=excluded.active_compaction_id,updated_at=excluded.updated_at""",
            (session_id, compaction_id, now()),
        )
        return _record(record)

    async def create(
        self,
        *,
        session_id: str,
        run_id: str,
        summary: dict[str, object],
        first_message_id: int | None,
        last_message_id: int | None,
        source_compaction_id: str | None,
        input_tokens: int,
        output_tokens: int,
        source_tokens: int,
        target_tokens: int,
        model_profile: str,
        source_digest: str,
        focus_instructions: str | None = None,
        activate: bool = False,
    ) -> CompactionRecord:
        identifier = f"compact_{uuid.uuid4().hex}"
        await self.execute(
            """INSERT INTO context_compactions(
                 id,session_id,run_id,first_message_id,last_message_id,summary_json,
                 source_compaction_id,input_tokens,output_tokens,source_tokens,
                 target_tokens,model_profile,source_digest,focus_instructions,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                session_id,
                run_id,
                first_message_id,
                last_message_id,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                source_compaction_id,
                input_tokens,
                output_tokens,
                source_tokens,
                target_tokens,
                model_profile,
                source_digest,
                focus_instructions,
                now(),
            ),
        )
        record = await self.matching(session_id, source_digest)
        assert record is not None
        if activate:
            await self.activate(session_id, record.id)
        return record


def _record(row) -> CompactionRecord:
    return CompactionRecord(
        str(row["id"]),
        str(row["session_id"]),
        json.loads(row["summary_json"]),
        str(row["source_digest"]),
        int(row["first_message_id"]) if row["first_message_id"] is not None else None,
        int(row["last_message_id"]) if row["last_message_id"] is not None else None,
        int(row["source_tokens"]),
        int(row["target_tokens"]),
        str(row["model_profile"]),
        int(row["input_tokens"]),
        int(row["output_tokens"]),
        str(row["focus_instructions"])
        if row["focus_instructions"] is not None
        else None,
        str(row["created_at"]),
    )
