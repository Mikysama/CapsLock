"""External embedding consent and request audit persistence."""

from __future__ import annotations

import json

from .core import Repository, timestamp


class EmbeddingAuditRepository(Repository):
    async def consent(
        self,
        *,
        workspace: str,
        provider: str,
        model: str,
        data_policy: str,
        fields: tuple[str, ...],
        record_count: int,
        byte_count: int,
        content_hash: str,
    ) -> int:
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE embedding_consents SET revoked_at=? WHERE workspace_key=? AND revoked_at IS NULL",
                (timestamp(), workspace),
            )
            cursor = await connection.execute(
                """INSERT INTO embedding_consents(workspace_key,provider,model,data_policy,fields_json,record_count,byte_count,content_hash,confirmed_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    workspace,
                    provider,
                    model,
                    data_policy,
                    json.dumps(fields, ensure_ascii=False),
                    record_count,
                    byte_count,
                    content_hash,
                    timestamp(),
                ),
            )
            return int(cursor.lastrowid)

    async def revoke(self, workspace: str) -> int:
        return await self.execute(
            "UPDATE embedding_consents SET revoked_at=? WHERE workspace_key=? AND revoked_at IS NULL",
            (timestamp(), workspace),
        )

    async def require_valid(
        self,
        identifier: int,
        *,
        workspace: str,
        provider: str,
        model: str,
        data_policy: str,
    ):
        row = await self.one(
            """SELECT * FROM embedding_consents WHERE id=? AND workspace_key=? AND provider=? AND model=?
               AND data_policy=? AND revoked_at IS NULL""",
            (identifier, workspace, provider, model, data_policy),
        )
        if row is None:
            raise ValueError("external embedding consent is missing or no longer valid")
        return row

    async def record_request(
        self,
        *,
        consent_id: int,
        workspace: str,
        run_id: str | None,
        operation: str,
        record_count: int,
        byte_count: int,
        duration_ms: int,
        input_tokens: int,
        cost_usd: float,
        error_code: str | None = None,
    ) -> None:
        await self.execute(
            """INSERT INTO embedding_requests(consent_id,workspace_key,run_id,operation,record_count,byte_count,duration_ms,input_tokens,cost_usd,status,error_code,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                consent_id,
                workspace,
                run_id,
                operation,
                record_count,
                byte_count,
                duration_ms,
                input_tokens,
                cost_usd,
                "failed" if error_code else "completed",
                error_code,
                timestamp(),
            ),
        )
