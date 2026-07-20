"""Shared async repository primitives."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import aiosqlite

from ..async_database import WorkspaceDatabase


def now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, database: WorkspaceDatabase) -> None:
        self.database = database

    async def one(
        self, query: str, values: tuple[object, ...] = ()
    ) -> aiosqlite.Row | None:
        return await self.database.fetch_one(query, values)

    async def all(
        self, query: str, values: tuple[object, ...] = ()
    ) -> list[aiosqlite.Row]:
        return await self.database.fetch_all(query, values)

    async def execute(self, query: str, values: tuple[object, ...] = ()) -> int:
        return await self.database.execute(query, values)


def json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
