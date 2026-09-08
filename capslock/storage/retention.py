"""Scheduled retention for operational and audit-only database records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..configuration.types import StorageSettings
from .async_database import MemoryDatabase, WorkspaceDatabase


_MAINTENANCE_KEY = "storage_maintenance_at"


async def run_retention_maintenance(
    workspace: WorkspaceDatabase,
    memory: MemoryDatabase,
    settings: StorageSettings,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run due database cleanup and return deletion counts by table."""
    if not settings.maintenance_enabled:
        return {}
    current = (now or datetime.now(UTC)).astimezone(UTC)
    operation_cutoff = current - timedelta(days=settings.operation_retention_days)
    audit_cutoff = current - timedelta(days=settings.audit_retention_days)
    interval = timedelta(hours=settings.maintenance_interval_hours)
    removed: dict[str, int] = {}
    if await _due(workspace, current, interval):
        removed.update(
            await _maintain_workspace(
                workspace, operation_cutoff, audit_cutoff, current
            )
        )
    if await _due(memory, current, interval):
        removed.update(
            await _maintain_memory(memory, operation_cutoff, audit_cutoff, current)
        )
    return removed


async def _due(database, now: datetime, interval: timedelta) -> bool:
    row = await database.fetch_one(
        "SELECT value FROM database_metadata WHERE key=?", (_MAINTENANCE_KEY,)
    )
    if row is None:
        return True
    try:
        previous = datetime.fromisoformat(str(row["value"]))
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
    except ValueError:
        return True
    return now - previous.astimezone(UTC) >= interval


async def _maintain_workspace(
    database: WorkspaceDatabase,
    operation_cutoff: datetime,
    audit_cutoff: datetime,
    now: datetime,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with database.transaction() as connection:
        cursor = await connection.execute(
            """DELETE FROM agent_mailbox
               WHERE status IN ('acknowledged','expired')
                 AND COALESCE(acknowledged_at,expires_at,created_at)<?""",
            (operation_cutoff.isoformat(),),
        )
        counts["agent_mailbox"] = int(cursor.rowcount)
        cursor = await connection.execute(
            "DELETE FROM budget_decisions WHERE created_at<?",
            (audit_cutoff.isoformat(),),
        )
        counts["budget_decisions"] = int(cursor.rowcount)
        await _mark_complete(connection, now)
    return counts


async def _maintain_memory(
    database: MemoryDatabase,
    operation_cutoff: datetime,
    audit_cutoff: datetime,
    now: datetime,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with database.transaction() as connection:
        cursor = await connection.execute(
            """DELETE FROM memory_jobs
               WHERE status IN ('completed','failed')
                 AND COALESCE(completed_at,created_at)<?""",
            (operation_cutoff.isoformat(),),
        )
        counts["memory_jobs"] = int(cursor.rowcount)
        cursor = await connection.execute(
            """DELETE FROM memory_extractions AS extraction
               WHERE COALESCE(extraction.completed_at,extraction.created_at)<?
                 AND NOT EXISTS (
                   SELECT 1 FROM memory_candidates AS candidate
                   WHERE candidate.extraction_id=extraction.id
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM memory_sources AS source
                   WHERE source.extraction_id=extraction.id
                 )""",
            (operation_cutoff.isoformat(),),
        )
        counts["memory_extractions"] = int(cursor.rowcount)
        cursor = await connection.execute(
            """DELETE FROM memory_recalls AS recall
               WHERE recall.created_at<?
                 AND EXISTS (
                   SELECT 1 FROM memory_recalls AS newer
                   WHERE newer.workspace_key=recall.workspace_key
                     AND newer.session_id=recall.session_id
                     AND (newer.created_at>recall.created_at OR
                          (newer.created_at=recall.created_at AND
                           newer.run_id>recall.run_id))
                 )""",
            (operation_cutoff.isoformat(),),
        )
        counts["memory_recalls"] = int(cursor.rowcount)
        for table in ("memory_audit", "embedding_requests"):
            cursor = await connection.execute(
                f"DELETE FROM {table} WHERE created_at<?",
                (audit_cutoff.isoformat(),),
            )
            counts[table] = int(cursor.rowcount)
        await _mark_complete(connection, now)
    return counts


async def _mark_complete(connection, now: datetime) -> None:
    await connection.execute(
        """INSERT INTO database_metadata(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (_MAINTENANCE_KEY, now.isoformat()),
    )
