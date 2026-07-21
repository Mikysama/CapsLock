"""Declarative database specifications and migration hooks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

MigrationHook = Callable[[aiosqlite.Connection], Awaitable[None]]


@dataclass(frozen=True)
class MigrationStep:
    sql: str
    pre_hook: MigrationHook | None = None
    post_hook: MigrationHook | None = None
    failure_hook: MigrationHook | None = None


@dataclass(frozen=True)
class DatabaseSpec:
    application_id: int
    schema_version: int
    schema: str
    label: str
    migrations: dict[int, MigrationStep]
