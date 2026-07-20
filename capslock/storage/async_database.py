"""Async SQLite ownership and strict v2 schema initialization."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Self

import aiosqlite

from .schema_v2 import (
    MEMORY_APPLICATION_ID,
    MEMORY_SCHEMA,
    MEMORY_SCHEMA_VERSION,
    WORKSPACE_APPLICATION_ID,
    WORKSPACE_SCHEMA,
    WORKSPACE_SCHEMA_VERSION,
)


class IncompatibleDatabaseError(RuntimeError):
    pass


class AsyncDatabase:
    application_id: int
    schema_version: int
    schema: str
    label: str

    def __init__(self, path: Path, connection: aiosqlite.Connection) -> None:
        self.path = path
        self.connection = connection
        self._transaction_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        resolved = Path(path).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(resolved)
        connection.row_factory = aiosqlite.Row
        instance = cls(resolved, connection)
        try:
            await instance._configure()
            await instance._initialize_or_validate()
            await instance._configure_validated()
        except Exception:
            await connection.close()
            raise
        return instance

    async def _configure(self) -> None:
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.execute("PRAGMA busy_timeout=5000")

    async def _configure_validated(self) -> None:
        await self.connection.execute("PRAGMA journal_mode=WAL")

    async def _initialize_or_validate(self) -> None:
        app_id = int(
            (await (await self.connection.execute("PRAGMA application_id")).fetchone())[
                0
            ]
        )
        version = int(
            (await (await self.connection.execute("PRAGMA user_version")).fetchone())[0]
        )
        rows = await (
            await self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ).fetchall()
        if app_id == 0 and not rows:
            try:
                await self.connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + self.schema
                    + f"\nPRAGMA application_id={self.application_id};"
                    + f"\nPRAGMA user_version={self.schema_version};"
                    + "\nCOMMIT;"
                )
            except BaseException:
                await self.connection.rollback()
                raise
            return
        if app_id != self.application_id or version != self.schema_version:
            raise IncompatibleDatabaseError(
                f"{self.label} database is not compatible with CapsLock v2: {self.path}; "
                "move it to a backup location and start again"
            )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._transaction_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                await self.connection.rollback()
                raise
            else:
                await self.connection.commit()

    async def fetch_one(
        self, query: str, values: tuple[object, ...] = ()
    ) -> aiosqlite.Row | None:
        async with self._transaction_lock:
            return await (await self.connection.execute(query, values)).fetchone()

    async def fetch_all(
        self, query: str, values: tuple[object, ...] = ()
    ) -> list[aiosqlite.Row]:
        async with self._transaction_lock:
            rows = await (await self.connection.execute(query, values)).fetchall()
            return list(rows)

    async def execute(self, query: str, values: tuple[object, ...] = ()) -> int:
        async with self.transaction() as connection:
            cursor = await connection.execute(query, values)
            return int(cursor.rowcount)

    async def close(self) -> None:
        await self.connection.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()


class WorkspaceDatabase(AsyncDatabase):
    application_id = WORKSPACE_APPLICATION_ID
    schema_version = WORKSPACE_SCHEMA_VERSION
    schema = WORKSPACE_SCHEMA
    label = "workspace"


class MemoryDatabase(AsyncDatabase):
    application_id = MEMORY_APPLICATION_ID
    schema_version = MEMORY_SCHEMA_VERSION
    schema = MEMORY_SCHEMA
    label = "memory"

    async def _configure_validated(self) -> None:
        await super()._configure_validated()
        await self.connection.execute("PRAGMA secure_delete=ON")

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        instance = await super().open(path)
        instance.path.chmod(0o600)
        return instance
