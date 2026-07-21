"""Shared memory visibility, search, and row-mapping primitives."""

from __future__ import annotations

import re
from typing import Any

from ...domain import MemoryInfo, MemoryOrigin, MemoryScope, MemoryStatus, MemoryType

MEMORY_COLUMNS = """m.id,m.scope,m.workspace_key,m.session_id,m.status,m.current_revision,m.origin,
 m.source_valid,m.created_at AS memory_created_at,m.updated_at,m.purged_at,
 r.content,r.memory_type,r.source_kind,r.source_ref,r.confidence,r.expires_at"""
SELECT_MEMORY = f"""SELECT {MEMORY_COLUMNS}
 FROM memories m LEFT JOIN memory_revisions r ON r.memory_id=m.id AND r.revision=m.current_revision"""


def visible_where(workspace: str, session_id: str) -> tuple[str, list[Any]]:
    return (
        "(m.scope='global' OR (m.scope='workspace' AND m.workspace_key=?) OR (m.scope='session' AND m.workspace_key=? AND m.session_id=?))",
        [workspace, workspace, session_id],
    )


def search_terms(query: str) -> list[str]:
    terms = [item.casefold() for item in re.findall(r"[A-Za-z0-9_]+", query)]
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(item for item in terms if len(item) >= 2))[:32] or [
        query.strip()
    ]


def fts_query(query: str) -> str:
    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in search_terms(query)
    )


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def memory_from_row(row) -> MemoryInfo:
    revision = int(row["current_revision"] or 0)
    return MemoryInfo(
        id=str(row["id"]),
        content=row["content"],
        type=MemoryType(row["memory_type"] or MemoryType.NOTE.value),
        scope=MemoryScope(row["scope"]),
        workspace_key=row["workspace_key"],
        session_id=row["session_id"],
        source_kind=str(row["source_kind"] or "purged"),
        source_ref=row["source_ref"],
        confidence=float(row["confidence"] or 0),
        expires_at=row["expires_at"],
        revision=revision,
        status=MemoryStatus(row["status"]),
        created_at=str(row["memory_created_at"]),
        updated_at=str(row["updated_at"]),
        purged_at=row["purged_at"],
        origin=MemoryOrigin(row["origin"]),
        source_valid=bool(row["source_valid"]),
    )
