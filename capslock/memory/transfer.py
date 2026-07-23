"""Version 3 memory import and export."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ..domain import MemoryOrigin, MemoryScope, MemoryType
from ..storage.memory_repositories import MemoryRepositories
from .validation import (
    MAX_TRANSFER_BYTES,
    MAX_TRANSFER_RECORDS,
    confidence,
    expiry,
    transfer_path,
    validated_text,
)

EXPORT_FORMAT = "capslock-memory-export"
EXPORT_VERSION = 3


class MemoryTransferService:
    def __init__(
        self,
        repositories: MemoryRepositories,
        *,
        workspace: Path,
        workspace_key: str,
        session_id: str,
        event,
    ) -> None:
        self.repositories, self.workspace, self.workspace_key = (
            repositories,
            workspace,
            workspace_key,
        )
        self.session_id, self.event = session_id, event

    async def export_json(
        self,
        scope: MemoryScope,
        requested_path: str,
        *,
        overwrite: bool = False,
        include_candidates: bool = False,
    ) -> tuple[Path, int]:
        path = transfer_path(self.workspace, requested_path, writing=True)
        if path.exists() and not overwrite:
            raise FileExistsError("export file already exists")
        items = await self.repositories.query.list_visible(
            workspace=self.workspace_key,
            session_id=self.session_id,
            scope=scope,
            limit=MAX_TRANSFER_RECORDS + 1,
        )
        if len(items) > MAX_TRANSFER_RECORDS:
            raise ValueError("memory export contains too many records")
        records = []
        for item in items:
            records.append(
                {
                    "type": item.type.value,
                    "content": item.content,
                    "confidence": item.confidence,
                    "expires_at": item.expires_at,
                    "origin": item.origin.value,
                    "sources": await self.repositories.sources.list(item.id),
                }
            )
        candidates = []
        if include_candidates:
            candidates = [
                {
                    "content": item.content,
                    "type": item.type.value,
                    "scope": item.scope.value,
                    "confidence": item.confidence,
                    "status": item.status.value,
                    "risk_flags": list(item.risk_flags),
                }
                for item in await self.repositories.candidates.list(
                    workspace=self.workspace_key,
                    session_id=self.session_id,
                    include_all=True,
                )
                if item.scope is scope
            ]
        document = {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "scope": scope.value,
            "records": records,
            "candidates": candidates,
        }
        encoded = (
            json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n"
        ).encode()
        if len(encoded) > MAX_TRANSFER_BYTES:
            raise ValueError("memory export exceeds the byte limit")
        await asyncio.to_thread(_atomic_write, path, encoded)
        await self.repositories.audit.record_export(
            workspace=self.workspace_key,
            session_id=self.session_id,
            scope=scope,
            count=len(records),
        )
        self.event("memory_exported", scope=scope.value, count=len(records))
        return path, len(records)

    async def import_json(
        self, scope: MemoryScope, requested_path: str
    ) -> tuple[list[object], tuple[str, ...]]:
        path = transfer_path(self.workspace, requested_path, writing=False)
        raw = await asyncio.to_thread(path.read_bytes)
        if len(raw) > MAX_TRANSFER_BYTES:
            raise ValueError("memory import exceeds the byte limit")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("memory import must be valid UTF-8 JSON") from exc
        if (
            not isinstance(document, dict)
            or document.get("format") != EXPORT_FORMAT
            or document.get("version") != EXPORT_VERSION
        ):
            raise ValueError("only CapsLock memory export version 3 is supported")
        records = document.get("records")
        if not isinstance(records, list) or len(records) > MAX_TRANSFER_RECORDS:
            raise ValueError("memory import records must be a bounded list")
        workspace, session_id = _scope_keys(scope, self.workspace_key, self.session_id)
        output, rules = [], []
        for record in records:
            if not isinstance(record, dict) or set(record) - {
                "type",
                "content",
                "confidence",
                "expires_at",
                "origin",
                "sources",
            }:
                raise ValueError("imported memory has invalid fields")
            safe, redactions = validated_text(record.get("content"))
            rules.extend(redactions)
            output.append(
                await self.repositories.lifecycle.create(
                    content=safe,
                    memory_type=MemoryType(record["type"]),
                    scope=scope,
                    workspace=workspace,
                    session_id=session_id,
                    source_kind="import",
                    source_ref=None,
                    confidence=confidence(record.get("confidence", 1)),
                    expires_at=expiry(record.get("expires_at")),
                    origin=MemoryOrigin.IMPORTED,
                    operation="import",
                )
            )
        self.event("memory_imported", scope=scope.value, count=len(output))
        return output, tuple(dict.fromkeys(rules))


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=False, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".capslock-memory-", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def _scope_keys(
    scope: MemoryScope, workspace: str, session_id: str
) -> tuple[str | None, str | None]:
    if scope is MemoryScope.GLOBAL:
        return None, None
    if scope is MemoryScope.WORKSPACE:
        return workspace, None
    return workspace, session_id
