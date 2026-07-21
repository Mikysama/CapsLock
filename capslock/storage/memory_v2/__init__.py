"""Composed async user-memory repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..async_database import MemoryDatabase
from .candidates import CandidateRepository
from .core import workspace_key
from .lifecycle import MemoryRepository
from .semantic import EmbeddingRepository, RecallRepository
from .external import EmbeddingAuditRepository
from .facades import (
    MemoryAuditRepository,
    MemoryLifecycleRepository,
    MemoryQueryRepository,
    MemorySettingsRepository,
    MemorySourceRepository,
)


@dataclass(frozen=True)
class MemoryRepositories:
    database: MemoryDatabase
    memories: MemoryRepository
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
        memories = MemoryRepository(database)
        return cls(
            database,
            memories,
            MemoryLifecycleRepository(memories),
            MemoryQueryRepository(memories),
            MemorySourceRepository(memories),
            MemorySettingsRepository(memories),
            MemoryAuditRepository(memories),
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
    "MemoryRepository",
    "MemoryLifecycleRepository",
    "MemoryQueryRepository",
    "MemorySourceRepository",
    "MemorySettingsRepository",
    "MemoryAuditRepository",
    "RecallRepository",
    "workspace_key",
]
