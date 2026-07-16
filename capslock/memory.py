"""Application service for safe, scoped, explicitly managed local memories."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import MemoryInfo, MemoryScope, MemoryType
from .layout import UserLayout
from .policy import PolicyError, WorkspacePolicy
from .security import sanitize_memory_text
from .storage.memory import MemoryStore, workspace_key


MAX_MEMORY_BYTES = 8 * 1024
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_RECORDS = 1_000
EXPORT_FORMAT = "capslock-memory-export"
EXPORT_VERSION = 1


def default_memory_database() -> Path:
    return UserLayout.from_environment().memory


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        *,
        workspace: Path,
        session_id: str,
        project_write_enabled: bool = True,
        event: Callable[..., None] | None = None,
    ) -> None:
        self.store = store
        self.workspace = workspace.resolve()
        self.workspace_key = workspace_key(self.workspace)
        self.session_id = session_id
        self.project_write_enabled = project_write_enabled
        self.event = event or (lambda *args, **kwargs: None)

    @property
    def local_write_enabled(self) -> bool:
        return self.store.local_write_enabled(self.workspace_key)

    @property
    def write_enabled(self) -> bool:
        return self.project_write_enabled and self.local_write_enabled

    def set_local_write_enabled(self, enabled: bool) -> None:
        self.store.set_local_write_enabled(self.workspace_key, enabled)
        self.event("memory_policy_changed", enabled=enabled, effective=self.write_enabled)

    def add(
        self,
        *,
        content: str,
        memory_type: MemoryType,
        scope: MemoryScope,
        confidence: float = 1.0,
        expires_at: str | None = None,
    ) -> tuple[MemoryInfo, tuple[str, ...]]:
        self._require_write()
        safe, rules = _validated_text(content)
        expiry = _expiry(expires_at)
        workspace, session = self._scope_keys(scope)
        item = self.store.create(
            content=safe,
            memory_type=memory_type,
            scope=scope,
            workspace=workspace,
            session_id=session,
            source_kind="manual",
            source_ref=self.session_id,
            confidence=_confidence(confidence),
            expires_at=expiry,
        )
        self.event("memory_added", memory_id=item.id, scope=item.scope.value, revision=item.revision)
        return item, rules

    def edit(
        self,
        prefix: str,
        *,
        content: str,
        memory_type: MemoryType,
        confidence: float,
        expires_at: str | None,
    ) -> tuple[MemoryInfo, tuple[str, ...]]:
        self._require_write()
        current = self.resolve(prefix)
        safe, rules = _validated_text(content)
        item = self.store.edit(
            current.id,
            content=safe,
            memory_type=memory_type,
            source_kind="manual",
            source_ref=self.session_id,
            confidence=_confidence(confidence),
            expires_at=_expiry(expires_at),
        )
        self.event("memory_edited", memory_id=item.id, scope=item.scope.value, revision=item.revision)
        return item, rules

    def forget(self, prefix: str) -> MemoryInfo:
        self._require_write()
        item = self.store.forget(self.resolve(prefix).id)
        self.event("memory_forgotten", memory_id=item.id, scope=item.scope.value, revision=item.revision)
        return item

    def undo(self, prefix: str) -> MemoryInfo:
        self._require_write()
        item = self.store.undo(self.resolve(prefix).id)
        self.event("memory_undone", memory_id=item.id, scope=item.scope.value, revision=item.revision)
        return item

    def purge(self, prefix: str) -> MemoryInfo:
        self._require_write()
        item = self.store.purge(self.resolve(prefix).id)
        self.event("memory_purged", memory_id=item.id, scope=item.scope.value, revision=item.revision)
        return item

    def resolve(self, prefix: str, *, include_inactive: bool = True) -> MemoryInfo:
        if not prefix:
            raise ValueError("provide a memory id")
        return self.store.resolve(
            prefix,
            workspace=self.workspace_key,
            session_id=self.session_id,
            include_inactive=include_inactive,
        )

    def list(
        self,
        *,
        scope: MemoryScope | None = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> list[MemoryInfo]:
        return self.store.list_visible(
            workspace=self.workspace_key,
            session_id=self.session_id,
            scope=scope,
            include_inactive=include_inactive,
            limit=limit,
        )

    def search(self, query: str, *, run_id: str | None = None, limit: int = 10) -> list[MemoryInfo]:
        query = query.strip()
        if not query:
            raise ValueError("memory search query must not be empty")
        if len(query) > 512:
            raise ValueError("memory search query exceeds 512 characters")
        items = self.store.search(
            query, workspace=self.workspace_key, session_id=self.session_id, limit=max(1, min(limit, 20))
        )
        if run_id and items:
            self.store.record_access(
                items, workspace=self.workspace_key, session_id=self.session_id, run_id=run_id
            )
        return items

    def get_for_model(self, prefix: str, *, run_id: str) -> MemoryInfo:
        item = self.resolve(prefix, include_inactive=False)
        self.store.record_access(
            [item], workspace=self.workspace_key, session_id=self.session_id, run_id=run_id
        )
        return item

    def excluded_runs(self) -> set[str]:
        return self.store.excluded_runs(workspace=self.workspace_key, session_id=self.session_id)

    def export_json(self, scope: MemoryScope, requested_path: str, *, overwrite: bool = False) -> tuple[Path, int]:
        path = _json_path(self.workspace, requested_path, writing=True)
        if path.exists() and not overwrite:
            raise FileExistsError("export file already exists")
        items = self.list(scope=scope, limit=MAX_IMPORT_RECORDS + 1)
        if len(items) > MAX_IMPORT_RECORDS:
            raise ValueError(f"memory export must contain at most {MAX_IMPORT_RECORDS} records")
        records = [
            {
                "type": item.type.value,
                "content": sanitize_memory_text(item.content or "")[0],
                "confidence": item.confidence,
                "expires_at": item.expires_at,
                "source": {"kind": item.source_kind, "ref": sanitize_memory_text(item.source_ref or "")[0] or None},
            }
            for item in items
        ]
        document = {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "scope": scope.value,
            "records": records,
        }
        encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
        if len(encoded) > MAX_IMPORT_BYTES:
            raise ValueError(f"memory export exceeds the {MAX_IMPORT_BYTES} byte limit")
        path.parent.mkdir(parents=False, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".capslock-memory-", delete=False) as handle:
                temporary = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()
        self.store.audit_export(
            workspace=self.workspace_key, session_id=self.session_id, scope=scope, count=len(records)
        )
        self.event("memory_exported", scope=scope.value, count=len(records))
        return path, len(records)

    def import_json(self, scope: MemoryScope, requested_path: str) -> tuple[list[MemoryInfo], tuple[str, ...]]:
        self._require_write()
        path = _json_path(self.workspace, requested_path, writing=False)
        size = path.stat().st_size
        if size > MAX_IMPORT_BYTES:
            raise ValueError(f"memory import exceeds the {MAX_IMPORT_BYTES} byte limit")
        raw = path.read_bytes()
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("memory import must be valid UTF-8 JSON") from exc
        if not isinstance(document, dict) or document.get("format") != EXPORT_FORMAT or document.get("version") != 1:
            raise ValueError("unsupported memory export format or version")
        records = document.get("records")
        if not isinstance(records, list) or len(records) > MAX_IMPORT_RECORDS:
            raise ValueError(f"memory import must contain at most {MAX_IMPORT_RECORDS} records")
        workspace, session = self._scope_keys(scope)
        fingerprint = hashlib.sha256(raw).hexdigest()
        prepared: list[dict[str, Any]] = []
        all_rules: list[str] = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("each imported memory must be an object")
            if set(record) - {"type", "content", "confidence", "expires_at", "source"}:
                raise ValueError("imported memory contains unknown fields")
            safe, rules = _validated_text(record.get("content"))
            all_rules.extend(rules)
            try:
                memory_type = MemoryType(record.get("type"))
            except ValueError as exc:
                raise ValueError("imported memory has an unsupported type") from exc
            prepared.append(
                {
                    "content": safe,
                    "memory_type": memory_type,
                    "scope": scope,
                    "workspace": workspace,
                    "session_id": session,
                    "source_kind": "import",
                    "source_ref": fingerprint,
                    "confidence": _confidence(record.get("confidence", 1.0)),
                    "expires_at": _expiry(record.get("expires_at")),
                }
            )
        items = self.store.import_many(prepared)
        self.event("memory_imported", scope=scope.value, count=len(items))
        return items, tuple(dict.fromkeys(all_rules))

    def _scope_keys(self, scope: MemoryScope) -> tuple[str | None, str | None]:
        if scope is MemoryScope.GLOBAL:
            return None, None
        if scope is MemoryScope.WORKSPACE:
            return self.workspace_key, None
        return self.workspace_key, self.session_id

    def _require_write(self) -> None:
        if not self.project_write_enabled:
            raise PermissionError("memory writes are disabled by capslock.toml")
        if not self.local_write_enabled:
            raise PermissionError("memory writes are disabled locally for this workspace")


def _validated_text(value: Any) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("memory content must be a non-empty string")
    safe, rules = sanitize_memory_text(value.strip())
    if len(safe.encode("utf-8")) > MAX_MEMORY_BYTES:
        raise ValueError(f"memory content exceeds the {MAX_MEMORY_BYTES} byte limit")
    return safe, rules


def _confidence(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("memory confidence must be a number from 0 to 1") from exc
    if not 0 <= result <= 1:
        raise ValueError("memory confidence must be a number from 0 to 1")
    return result


def _expiry(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("memory expiry must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("memory expiry must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("memory expiry must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _json_path(workspace: Path, requested_path: str, *, writing: bool) -> Path:
    if not requested_path:
        raise ValueError("provide a workspace-relative JSON path")
    relative = Path(requested_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise PolicyError("memory import/export requires a workspace-relative path")
    unresolved = workspace
    for part in relative.parts:
        unresolved /= part
        if unresolved.is_symlink():
            raise PolicyError("memory import/export does not follow symbolic links")
    policy = WorkspacePolicy(workspace, max_file_bytes=MAX_IMPORT_BYTES)
    path = policy.resolve(requested_path)
    if path.suffix.casefold() != ".json":
        raise PolicyError("memory import/export path must end in .json")
    if writing:
        policy.writable_file(requested_path, create=not path.exists())
    else:
        policy.readable_file(requested_path)
    return path
