"""Async SQLite ownership and strict schema initialization."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Self

import aiosqlite

from .specs import DatabaseSpec, MEMORY_DATABASE_SPEC, WORKSPACE_DATABASE_SPEC


class IncompatibleDatabaseError(RuntimeError):
    pass


class AsyncDatabase:
    spec: DatabaseSpec
    application_id: int
    schema_version: int
    schema: str
    label: str

    def __init__(self, path: Path, connection: aiosqlite.Connection) -> None:
        self.path = path
        self.connection = connection
        self._transaction_lock = asyncio.Lock()
        self._readers: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()

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
            await instance._open_readers()
        except Exception:
            await connection.close()
            raise
        return instance

    async def _configure(self) -> None:
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.execute("PRAGMA busy_timeout=5000")

    async def _configure_validated(self) -> None:
        cursor = await self.connection.execute("PRAGMA journal_mode=WAL")
        await cursor.close()
        await self.connection.commit()

    async def _open_readers(self, size: int = 2) -> None:
        for _ in range(size):
            reader = await aiosqlite.connect(f"file:{self.path}?mode=ro", uri=True)
            reader.row_factory = aiosqlite.Row
            foreign_keys = await reader.execute("PRAGMA foreign_keys=ON")
            await foreign_keys.close()
            timeout = await reader.execute("PRAGMA busy_timeout=5000")
            await timeout.close()
            await self._readers.put(reader)

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
                f"{self.label} database schema is not supported: {self.path}"
            )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._transaction_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                await _finish_database_operation(self.connection.rollback())
                raise
            else:
                await _finish_database_operation(self.connection.commit())

    async def fetch_one(
        self, query: str, values: tuple[object, ...] = ()
    ) -> aiosqlite.Row | None:
        reader = await self._readers.get()
        try:
            cursor = await reader.execute(query, values)
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()
        finally:
            await self._readers.put(reader)

    async def fetch_all(
        self, query: str, values: tuple[object, ...] = ()
    ) -> list[aiosqlite.Row]:
        reader = await self._readers.get()
        try:
            cursor = await reader.execute(query, values)
            try:
                rows = await cursor.fetchall()
                return list(rows)
            finally:
                await cursor.close()
        finally:
            await self._readers.put(reader)

    async def execute(self, query: str, values: tuple[object, ...] = ()) -> int:
        async with self.transaction() as connection:
            cursor = await connection.execute(query, values)
            return int(cursor.rowcount)

    async def close(self) -> None:
        while not self._readers.empty():
            reader = await self._readers.get()
            await reader.close()
        await self.connection.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()


class WorkspaceDatabase(AsyncDatabase):
    spec = WORKSPACE_DATABASE_SPEC
    application_id = spec.application_id
    schema_version = spec.schema_version
    schema = spec.schema
    label = spec.label


class MemoryDatabase(AsyncDatabase):
    spec = MEMORY_DATABASE_SPEC
    application_id = spec.application_id
    schema_version = spec.schema_version
    schema = spec.schema
    label = spec.label

    async def _configure_validated(self) -> None:
        await super()._configure_validated()
        await self.connection.execute("PRAGMA secure_delete=ON")

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        instance = await super().open(path)
        instance.path.chmod(0o600)
        return instance


async def _finish_database_operation(operation) -> None:
    task = asyncio.create_task(operation)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
