"""Async user-memory repository primitives."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from ..async_database import MemoryDatabase


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def workspace_key(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()


class Repository:
    def __init__(self, database: MemoryDatabase) -> None:
        self.database = database

    async def one(self, query: str, values: tuple[object, ...] = ()):
        return await self.database.fetch_one(query, values)

    async def all(self, query: str, values: tuple[object, ...] = ()):
        return await self.database.fetch_all(query, values)

    async def execute(self, query: str, values: tuple[object, ...] = ()) -> int:
        return await self.database.execute(query, values)
