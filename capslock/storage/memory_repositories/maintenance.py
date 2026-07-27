"""Transactional deterministic memory consolidation primitives."""

from __future__ import annotations

import difflib
import json
import re
import uuid

from .core import Repository, timestamp


class MemoryMaintenanceRepository(Repository):
    async def snapshot(
        self, workspace: str, *, limit: int = 200
    ) -> list[dict[str, object]]:
        rows = await self.all(
            """SELECT m.id,m.origin,m.scope,m.source_valid,r.content,r.memory_type,
                      r.confidence,r.subject,r.why,r.how_to_apply
               FROM memories m JOIN memory_revisions r
                 ON r.memory_id=m.id AND r.revision=m.current_revision
               WHERE m.workspace_key=? AND m.status='active'
               ORDER BY m.updated_at LIMIT ?""",
            (workspace, min(limit, 200)),
        )
        return [dict(row) for row in rows]

    async def reviews(
        self, workspace: str, *, limit: int = 100
    ) -> list[dict[str, object]]:
        rows = await self.all(
            """SELECT r.* FROM memory_relations r
               JOIN memories m ON m.id=r.source_memory_id
               WHERE m.workspace_key=? AND r.status='pending'
               ORDER BY r.created_at LIMIT ?""",
            (workspace, limit),
        )
        output = [dict(row) for row in rows]
        remaining = max(0, limit - len(output))
        if remaining:
            proposals = await self.all(
                """SELECT * FROM memory_review_proposals
                   WHERE workspace_key=? AND status='pending'
                   ORDER BY created_at LIMIT ?""",
                (workspace, remaining),
            )
            output.extend(dict(row) for row in proposals)
        return output

    async def store_model_proposals(
        self,
        workspace: str,
        job_id: str,
        proposals: list[dict[str, object]],
    ) -> int:
        created = timestamp()
        async with self.database.transaction() as connection:
            for proposal in proposals:
                kind = str(proposal["type"])
                identifiers = list(proposal["memory_ids"])
                if kind in {"duplicate", "conflict", "supersedes"}:
                    relation = kind
                    await connection.execute(
                        """INSERT OR IGNORE INTO memory_relations(
                           source_memory_id,target_memory_id,relation,status,confidence,
                           created_by,created_at) VALUES(?,?,?,'pending',?,'model_consolidation',?)""",
                        (
                            identifiers[0],
                            identifiers[1],
                            relation,
                            proposal["confidence"],
                            created,
                        ),
                    )
                else:
                    await connection.execute(
                        """INSERT INTO memory_review_proposals(
                           id,workspace_key,proposal_type,memory_ids_json,proposed_content,
                           confidence,payload_json,job_id,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            f"mreview_{uuid.uuid4().hex}",
                            workspace,
                            kind,
                            json.dumps(identifiers),
                            proposal.get("proposed_content"),
                            proposal["confidence"],
                            json.dumps(proposal, ensure_ascii=False, sort_keys=True),
                            job_id,
                            created,
                        ),
                    )
        return len(proposals)

    async def status(self, workspace: str) -> dict[str, int | str | None]:
        row = await self.one(
            """SELECT
               (SELECT count(*) FROM memories WHERE workspace_key=? AND status='active') active,
               ((SELECT count(*) FROM memory_relations r JOIN memories m ON m.id=r.source_memory_id
                 WHERE m.workspace_key=? AND r.status='pending' AND r.relation='conflict') +
                (SELECT count(*) FROM memory_candidates
                 WHERE workspace_key=? AND status='conflict')) conflicts,
               (SELECT last_consolidated_at FROM memory_maintenance_state WHERE workspace_key=?) last_run""",
            (workspace, workspace, workspace, workspace),
        )
        assert row is not None
        state = await self.one(
            "SELECT completed_sessions_at_last_run FROM memory_maintenance_state WHERE workspace_key=?",
            (workspace,),
        )
        return {
            "active": int(row["active"]),
            "pending_conflicts": int(row["conflicts"]),
            "last_run": row["last_run"],
            "completed_sessions_at_last_run": int(state[0]) if state else 0,
        }

    async def consolidate(
        self, workspace: str, *, limit: int = 200, completed_sessions: int = 0
    ) -> dict[str, int]:
        rows = await self.all(
            """SELECT m.id,m.origin,m.source_valid,m.current_revision,r.content,r.memory_type
               FROM memories m JOIN memory_revisions r ON r.memory_id=m.id AND r.revision=m.current_revision
               WHERE m.workspace_key=? AND m.status='active' ORDER BY m.updated_at LIMIT ?""",
            (workspace, min(limit, 200)),
        )
        groups: dict[str, list[object]] = {}
        invalid: list[object] = []
        for row in rows:
            key = re.sub(r"\s+", " ", str(row["content"]).casefold()).strip()
            groups.setdefault(key, []).append(row)
            if row["origin"] == "automatic" and not bool(row["source_valid"]):
                invalid.append(row)
        duplicate_pairs: list[tuple[str, object]] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            survivor = next(
                (item for item in group if item["origin"] != "automatic"), group[0]
            )
            duplicate_pairs.extend(
                (str(survivor["id"]), item)
                for item in group
                if item["id"] != survivor["id"] and item["origin"] == "automatic"
            )
        near_pairs: list[tuple[str, str]] = []
        for index, left in enumerate(rows):
            left_text = re.sub(r"\s+", " ", str(left["content"]).casefold()).strip()
            for right in rows[index + 1 :]:
                right_text = re.sub(
                    r"\s+", " ", str(right["content"]).casefold()
                ).strip()
                if (
                    left["memory_type"] == right["memory_type"]
                    and left_text != right_text
                    and min(len(left_text), len(right_text)) >= 40
                    and difflib.SequenceMatcher(None, left_text, right_text).ratio()
                    >= 0.90
                ):
                    near_pairs.append(
                        tuple(sorted((str(left["id"]), str(right["id"]))))
                    )
        forgotten = {str(row["id"]): row for row in invalid}
        forgotten.update({str(row["id"]): row for _, row in duplicate_pairs})
        changed = timestamp()
        async with self.database.transaction() as connection:
            for source, target in near_pairs:
                await connection.execute(
                    """INSERT OR IGNORE INTO memory_relations(source_memory_id,target_memory_id,
                       relation,status,confidence,created_by,created_at)
                       VALUES(?,?,'duplicate','pending',0.9,'consolidation',?)""",
                    (source, target, changed),
                )
            for survivor, duplicate in duplicate_pairs:
                await connection.execute(
                    """INSERT OR IGNORE INTO memory_relations(source_memory_id,target_memory_id,
                       relation,status,confidence,created_by,created_at,decided_at)
                       VALUES(?,?,'duplicate','confirmed',1,'consolidation',?,?)""",
                    (duplicate["id"], survivor, changed, changed),
                )
                await connection.execute(
                    """INSERT OR IGNORE INTO memory_sources(memory_id,source_kind,source_ref,
                       extraction_id,workspace_key,session_id,run_id,message_id,evidence_id,
                       quote,direct,verified,valid,created_at,invalidated_at)
                       SELECT ?,source_kind,source_ref,extraction_id,workspace_key,session_id,
                       run_id,message_id,evidence_id,quote,direct,verified,valid,created_at,
                       invalidated_at FROM memory_sources WHERE memory_id=?""",
                    (survivor, duplicate["id"]),
                )
            for identifier, row in forgotten.items():
                revision = int(row["current_revision"]) + 1
                await connection.execute(
                    """INSERT INTO memory_revisions(memory_id,revision,operation,content,
                       memory_type,source_kind,source_ref,confidence,expires_at,subject,
                       durability,why,how_to_apply,last_verified_at,created_at)
                       SELECT memory_id,?,'forget',content,memory_type,source_kind,source_ref,
                       confidence,expires_at,subject,durability,why,how_to_apply,last_verified_at,?
                       FROM memory_revisions WHERE memory_id=? AND revision=?""",
                    (revision, changed, identifier, row["current_revision"]),
                )
                await connection.execute(
                    "UPDATE memories SET status='forgotten',current_revision=?,updated_at=? WHERE id=?",
                    (revision, changed, identifier),
                )
                await connection.execute(
                    "DELETE FROM memory_fts WHERE memory_id=?", (identifier,)
                )
                await connection.execute(
                    """INSERT INTO memory_audit(memory_id,operation,workspace_key,revision,detail,created_at)
                       VALUES(?,'consolidate_forget',?,?,?,?)""",
                    (
                        identifier,
                        workspace,
                        revision,
                        "duplicate_or_invalid_automatic",
                        changed,
                    ),
                )
            await connection.execute(
                """INSERT INTO memory_maintenance_state(
                   workspace_key,last_consolidated_at,completed_sessions_at_last_run)
                   VALUES(?,?,?) ON CONFLICT(workspace_key) DO UPDATE SET
                   last_consolidated_at=excluded.last_consolidated_at,
                   completed_sessions_at_last_run=excluded.completed_sessions_at_last_run""",
                (workspace, changed, completed_sessions),
            )
        return {
            "processed": len(rows),
            "merged": len(duplicate_pairs),
            "forgotten": len(forgotten),
            "review_proposals": len(near_pairs),
        }
