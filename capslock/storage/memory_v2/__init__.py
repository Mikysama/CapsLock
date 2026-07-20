"""Composed async user-memory repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..async_database import MemoryDatabase
from .candidates import CandidateRepository
from .core import workspace_key
from .lifecycle import MemoryRepository
from .semantic import EmbeddingRepository, RecallRepository


@dataclass(frozen=True)
class MemoryRepositories:
    database: MemoryDatabase
    memories: MemoryRepository
    candidates: CandidateRepository
    embeddings: EmbeddingRepository
    recalls: RecallRepository

    @classmethod
    async def open(cls, path: str | Path) -> "MemoryRepositories":
        database = await MemoryDatabase.open(path)
        return cls(
            database,
            MemoryRepository(database),
            CandidateRepository(database),
            EmbeddingRepository(database),
            RecallRepository(database),
        )

    async def close(self) -> None:
        await self.database.close()


__all__ = [
    "CandidateRepository",
    "EmbeddingRepository",
    "MemoryRepositories",
    "MemoryRepository",
    "RecallRepository",
    "workspace_key",
]
