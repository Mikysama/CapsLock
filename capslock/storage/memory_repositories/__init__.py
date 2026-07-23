"""Composed async user-memory repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..async_database import MemoryDatabase
from .candidates import CandidateRepository
from .core import workspace_key
from .lifecycle import MemoryLifecycleRepository
from .query import MemoryQueryRepository
from .sources import MemorySourceRepository
from .settings import MemorySettingsRepository
from .audit import MemoryAuditRepository
from .semantic import EmbeddingRepository, RecallRepository
from .external import EmbeddingAuditRepository


@dataclass(frozen=True)
class MemoryRepositories:
    database: MemoryDatabase
    lifecycle: MemoryLifecycleRepository
    query: MemoryQueryRepository
    sources: MemorySourceRepository
    settings: MemorySettingsRepository
    audit: MemoryAuditRepository
    candidates: CandidateRepository
    embeddings: EmbeddingRepository
    recalls: RecallRepository
    embedding_audit: EmbeddingAuditRepository

    @classmethod
    async def open(cls, path: str | Path) -> "MemoryRepositories":
        database = await MemoryDatabase.open(path)
        query = MemoryQueryRepository(database)
        lifecycle = MemoryLifecycleRepository(database, query)
        return cls(
            database,
            lifecycle,
            query,
            MemorySourceRepository(database),
            MemorySettingsRepository(database),
            MemoryAuditRepository(database),
            CandidateRepository(database),
            EmbeddingRepository(database),
            RecallRepository(database),
            EmbeddingAuditRepository(database),
        )

    async def close(self) -> None:
        await self.database.close()


__all__ = [
    "CandidateRepository",
    "EmbeddingRepository",
    "EmbeddingAuditRepository",
    "MemoryRepositories",
    "MemoryLifecycleRepository",
    "MemoryQueryRepository",
    "MemorySourceRepository",
    "MemorySettingsRepository",
    "MemoryAuditRepository",
    "RecallRepository",
    "workspace_key",
]
