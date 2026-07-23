"""Memory audit persistence shared by transactional repositories."""

from __future__ import annotations

from ...domain import MemoryScope
from .core import Repository, timestamp


async def record_memory_audit(
    connection,
    memory_id: str,
    operation: str,
    scope: MemoryScope,
    workspace: str | None,
    session_id: str | None,
    revision: int,
) -> None:
    await connection.execute(
        "INSERT INTO memory_audit(memory_id,operation,scope,workspace_key,session_id,revision,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            memory_id,
            operation,
            scope.value,
            workspace,
            session_id,
            revision,
            timestamp(),
        ),
    )


class MemoryAuditRepository(Repository):
    async def record_export(
        self, *, workspace: str, session_id: str, scope: MemoryScope, count: int
    ) -> None:
        await self.execute(
            "INSERT INTO memory_audit(operation,scope,workspace_key,session_id,detail,created_at) VALUES('export',?,?,?,?,?)",
            (scope.value, workspace, session_id, f"count={count}", timestamp()),
        )
