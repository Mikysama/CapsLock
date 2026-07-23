"""Workspace and memory database specifications."""

from __future__ import annotations

from dataclasses import dataclass

from .schema import (
    MEMORY_APPLICATION_ID,
    MEMORY_SCHEMA,
    MEMORY_SCHEMA_VERSION,
    WORKSPACE_APPLICATION_ID,
    WORKSPACE_SCHEMA,
    WORKSPACE_SCHEMA_VERSION,
)


@dataclass(frozen=True)
class DatabaseSpec:
    application_id: int
    schema_version: int
    schema: str
    label: str


WORKSPACE_DATABASE_SPEC = DatabaseSpec(
    WORKSPACE_APPLICATION_ID,
    WORKSPACE_SCHEMA_VERSION,
    WORKSPACE_SCHEMA,
    "workspace",
)

MEMORY_DATABASE_SPEC = DatabaseSpec(
    MEMORY_APPLICATION_ID,
    MEMORY_SCHEMA_VERSION,
    MEMORY_SCHEMA,
    "memory",
)
