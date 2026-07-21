"""Workspace and memory database specifications."""

from __future__ import annotations

import aiosqlite

from .migrations import DatabaseSpec, MigrationStep
from .schema_v2 import (
    MEMORY_APPLICATION_ID,
    MEMORY_MIGRATIONS,
    MEMORY_SCHEMA,
    MEMORY_SCHEMA_VERSION,
    WORKSPACE_APPLICATION_ID,
    WORKSPACE_MIGRATIONS,
    WORKSPACE_SCHEMA,
    WORKSPACE_SCHEMA_VERSION,
)


async def _ensure_import_action_columns(connection: aiosqlite.Connection) -> None:
    columns = {
        str(row[1])
        for row in await (
            await connection.execute("PRAGMA table_info(actions)")
        ).fetchall()
    }
    for name, definition in (
        ("import_id", "TEXT REFERENCES lifecycle_imports(id) ON DELETE SET NULL"),
        (
            "historical_only",
            "INTEGER NOT NULL DEFAULT 0 CHECK(historical_only IN (0,1))",
        ),
        (
            "requires_reapproval",
            "INTEGER NOT NULL DEFAULT 0 CHECK(requires_reapproval IN (0,1))",
        ),
    ):
        if name not in columns:
            await connection.execute(
                f"ALTER TABLE actions ADD COLUMN {name} {definition}"
            )


async def _disable_foreign_keys(connection: aiosqlite.Connection) -> None:
    await connection.execute("PRAGMA foreign_keys=OFF")


async def _enable_foreign_keys(connection: aiosqlite.Connection) -> None:
    await connection.execute("PRAGMA foreign_keys=ON")


async def _validate_foreign_keys(connection: aiosqlite.Connection) -> None:
    await _enable_foreign_keys(connection)
    violations = await (await connection.execute("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise ValueError("workspace schema migration produced invalid references")


WORKSPACE_DATABASE_SPEC = DatabaseSpec(
    WORKSPACE_APPLICATION_ID,
    WORKSPACE_SCHEMA_VERSION,
    WORKSPACE_SCHEMA,
    "workspace",
    {
        version: MigrationStep(
            sql,
            pre_hook=(
                _ensure_import_action_columns
                if version == 2
                else _disable_foreign_keys
                if version == 3
                else None
            ),
            post_hook=_validate_foreign_keys if version == 3 else None,
            failure_hook=_enable_foreign_keys if version == 3 else None,
        )
        for version, sql in WORKSPACE_MIGRATIONS.items()
    },
)

MEMORY_DATABASE_SPEC = DatabaseSpec(
    MEMORY_APPLICATION_ID,
    MEMORY_SCHEMA_VERSION,
    MEMORY_SCHEMA,
    "memory",
    {version: MigrationStep(sql) for version, sql in MEMORY_MIGRATIONS.items()},
)
