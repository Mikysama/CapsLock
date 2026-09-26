"""Async SQLite ownership and strict schema initialization."""

from __future__ import annotations

import asyncio
import time
import json
import uuid
from datetime import UTC, datetime
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
        self._commit_samples = 0
        self._commit_total_ms = 0.0
        self._commit_maximum_ms = 0.0

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
        except BaseException:
            while not instance._readers.empty():
                reader = await instance._readers.get()
                await reader.close()
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
        if (
            self.label == "workspace"
            and app_id == self.application_id
            and version in {6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}
            and self.schema_version == 22
        ):
            from .upgrades import upgrade_workspace_schema

            await upgrade_workspace_schema(
                self.path, self.connection, source_version=version
            )
            return
        if (
            self.label == "memory"
            and app_id == self.application_id
            and version in {3, 4, 5}
            and self.schema_version == 6
        ):
            from .upgrades import upgrade_memory_schema

            await upgrade_memory_schema(
                self.path, self.connection, source_version=version
            )
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
                started = time.perf_counter()
                await _finish_database_operation(self.connection.commit())
                duration = (time.perf_counter() - started) * 1000
                self._commit_samples += 1
                self._commit_total_ms += duration
                self._commit_maximum_ms = max(self._commit_maximum_ms, duration)

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

    async def flush_commit_timings(self) -> None:
        if self.label != "workspace" or not self._commit_samples:
            return
        samples, total, maximum = (
            self._commit_samples,
            self._commit_total_ms,
            self._commit_maximum_ms,
        )
        self._commit_samples = 0
        self._commit_total_ms = self._commit_maximum_ms = 0.0
        # Persist one aggregate per flush rather than recursively tracing each write.
        try:
            async with self._transaction_lock:
                await self.connection.execute(
                    "INSERT INTO performance_spans(id,trace_id,category,name,status,duration_ms,attributes_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        f"span_{uuid.uuid4().hex}",
                        "database",
                        "database",
                        "commit",
                        "ok",
                        total,
                        json.dumps({"samples": samples, "maximum_ms": maximum}),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                await _finish_database_operation(self.connection.commit())
        except Exception:
            self._commit_samples += samples
            self._commit_total_ms += total
            self._commit_maximum_ms = max(self._commit_maximum_ms, maximum)
            await _finish_database_operation(self.connection.rollback())

    async def close(self) -> None:
        await self.flush_commit_timings()
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

    @classmethod
    async def open(cls, path: str | Path, *, shared_owner: bool = False) -> Self:
        from .ownership import acquire_workspace_lease, release_workspace_lease

        lease, recovery_owner = acquire_workspace_lease(
            Path(path).expanduser(), shared_owner=shared_owner
        )
        try:
            instance = await super().open(path)
        except BaseException:
            release_workspace_lease(lease)
            raise
        instance._owner_lease = lease
        instance.recovery_owner = recovery_owner
        lease.ready = True
        return instance

    async def close(self) -> None:
        from .ownership import release_workspace_lease

        lease = getattr(self, "_owner_lease", None)
        if lease is None:
            return
        try:
            await super().close()
        finally:
            self._owner_lease = None
            release_workspace_lease(lease)


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
