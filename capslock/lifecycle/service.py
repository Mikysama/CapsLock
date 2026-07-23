"""Lifecycle service orchestration and compatibility facade implementation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..layout import ProjectLayout
from ..storage.memory_repositories import workspace_key
from .archive import (
    MAX_ARCHIVE_BYTES,
    build_manifest,
    extract_zip as _extract_zip,
    read_json as _read_json,
    verify_archive,
    write_json as _write_json,
    write_zip as _write_zip,
)
from .errors import LifecycleError
from .coordinator import ImportCoordinator
from .backup import BackupService
from .io import LifecycleIO
from .sanitization import (
    redact_portable as _redact_portable,
    sanitize_mcp as _sanitize_mcp,
)
from .specs import (
    MEMORY_TABLES,
    WORKSPACE_TABLES,
)


EXPORT_FORMAT = "capslock-lifecycle-export"
ARCHIVE_VERSION = 2
SUPPORTED_ARCHIVE_VERSIONS = frozenset({1, ARCHIVE_VERSION})
MAX_ARCHIVE_RECORDS = 100_000


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class PortableArchiveService:
    def __init__(
        self,
        layout: ProjectLayout,
        backup: BackupService,
        io: LifecycleIO,
    ) -> None:
        self.layout = layout
        self.workspace = layout.workspace
        self.memory_path = layout.user.memory
        self.backup = backup
        self.io = io

    def export(self, destination: Path, *, include_global_memory: bool = False) -> Path:
        target = destination.expanduser().resolve()
        if target.exists():
            raise FileExistsError(f"export already exists: {target}")
        with (
            self.io.locks(),
            tempfile.TemporaryDirectory(prefix="capslock-export-") as raw,
        ):
            stage = Path(raw)
            archive_id = uuid.uuid4().hex
            workspace_rows = _database_rows(self.layout.database, WORKSPACE_TABLES)
            for task in workspace_rows.get("agent_tasks", []):
                # Temporary child paths are host-local and must never enter archives.
                task["child_workspace"] = None
            memory_rows = self._memory_rows(include_global_memory)
            document = _redact_portable(
                {
                    "archive_id": archive_id,
                    "workspace_key": workspace_key(self.workspace),
                    "workspace": workspace_rows,
                    "memory": memory_rows,
                }
            )
            _write_json(stage / "data.json", document)
            if (stage / "data.json").stat().st_size > MAX_ARCHIVE_BYTES:
                raise LifecycleError("portable export exceeds the size limit")
            mcp = {
                "project": _read_json(self.layout.project_mcp)
                if self.layout.project_mcp.is_file()
                else {},
                "local": _sanitize_mcp(_read_json(self.layout.local_mcp))
                if self.layout.local_mcp.is_file()
                else {},
            }
            _write_json(stage / "mcp.json", _redact_portable(mcp))
            manifest = build_manifest(
                EXPORT_FORMAT,
                stage,
                workspace=self.workspace,
                version=ARCHIVE_VERSION,
                extra={
                    "archive_id": archive_id,
                    "include_global_memory": include_global_memory,
                },
            )
            _write_json(stage / "manifest.json", manifest)
            _write_zip(stage, target)
        target.chmod(0o600)
        return target

    def import_archive(self, archive: Path) -> dict[str, Any]:
        archive = archive.expanduser().resolve()
        if archive.is_dir():
            return self._import_legacy_session(archive / "session.json")
        if archive.suffix.casefold() == ".json":
            return self._import_legacy_memory(archive)
        manifest = verify_archive(
            archive,
            supported_versions=SUPPORTED_ARCHIVE_VERSIONS,
            expected_format=EXPORT_FORMAT,
        )
        with tempfile.TemporaryDirectory(prefix="capslock-import-") as raw:
            stage = Path(raw)
            _extract_zip(archive, stage)
            document = _read_json(stage / "data.json")
            archive_id = str(document.get("archive_id", ""))
            if not archive_id or archive_id != manifest.get("archive_id"):
                raise LifecycleError("archive identity mismatch")
            data_workspace = document.get("workspace")
            data_memory = document.get("memory")
            if not isinstance(data_workspace, dict) or not isinstance(
                data_memory, dict
            ):
                raise LifecycleError("portable archive has invalid data sections")
            if set(data_workspace) - set(WORKSPACE_TABLES) or set(data_memory) - set(
                MEMORY_TABLES
            ):
                raise LifecycleError("portable archive contains unknown data tables")
            record_count = sum(
                len(records)
                for section in (data_workspace, data_memory)
                for records in section.values()
                if isinstance(records, list)
            )
            if record_count > MAX_ARCHIVE_RECORDS:
                raise LifecycleError("portable archive contains too many records")
            report = self._merge(
                archive_id,
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                str(manifest.get("source_version", "unknown")),
                data_workspace,
                data_memory,
            )
            self._merge_mcp(_read_json(stage / "mcp.json"), archive_id, report)
            self._persist_import_report(archive_id, report)
            return report

    def _merge(
        self,
        archive_id: str,
        archive_hash: str,
        source_version: str,
        workspace_rows: dict[str, Any],
        memory_rows: dict[str, Any],
    ) -> dict[str, Any]:
        return ImportCoordinator(
            layout=self.layout,
            memory_path=self.memory_path,
            workspace=self.workspace,
            backup_create=self.backup.create,
            locks=self.io.locks,
            journal=self.io.journal,
        ).merge(
            archive_id,
            archive_hash,
            source_version,
            workspace_rows,
            memory_rows,
        )

    def _memory_rows(self, include_global: bool) -> dict[str, list[dict[str, Any]]]:
        if not self.memory_path.exists():
            return {table: [] for table in MEMORY_TABLES}
        connection = sqlite3.connect(self.memory_path)
        connection.row_factory = sqlite3.Row
        key = workspace_key(self.workspace)
        try:
            clauses = "workspace_key=?" + (
                " OR scope='global'" if include_global else ""
            )
            memories = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM memories WHERE {clauses}", (key,)
                )
            ]
            memory_ids = {str(item["id"]) for item in memories}
            rows: dict[str, list[dict[str, Any]]] = {"memories": memories}
            for table in MEMORY_TABLES[1:]:
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if "memory_id" in columns:
                    rows[table] = _select_in(connection, table, "memory_id", memory_ids)
                elif "workspace_key" in columns:
                    rows[table] = [
                        dict(row)
                        for row in connection.execute(
                            f"SELECT * FROM {table} WHERE workspace_key=?", (key,)
                        )
                    ]
                else:
                    rows[table] = []
            extraction_ids = {
                str(item["id"]) for item in rows.get("memory_extractions", [])
            }
            if extraction_ids:
                rows["memory_candidates"] = _select_in(
                    connection, "memory_candidates", "extraction_id", extraction_ids
                )
            return rows
        finally:
            connection.close()

    def _merge_mcp(
        self, document: dict[str, Any], archive_id: str, report: dict[str, Any]
    ) -> None:
        imported_project = document.get("project", {})
        imported_local = document.get("local", {})
        if not isinstance(imported_project, dict) or not isinstance(
            imported_local, dict
        ):
            raise LifecycleError("portable MCP data must be objects")
        current = (
            _read_json(self.layout.project_mcp)
            if self.layout.project_mcp.is_file()
            else {"servers": {}}
        )
        current_servers = current.setdefault("servers", {})
        imported_servers: dict[str, Any] = {}
        for source in (imported_project, imported_local):
            servers = source.get("servers", {})
            if not isinstance(servers, dict):
                raise LifecycleError("portable MCP servers must be an object")
            for name, server in servers.items():
                if isinstance(server, dict):
                    imported_servers[str(name)] = {
                        **imported_servers.get(str(name), {}),
                        **server,
                    }
        server_mappings: dict[str, str] = {}
        for name, server in imported_servers.items():
            if not isinstance(server, dict):
                report["blocked"] += 1
                continue
            safe = dict(server)
            safe.pop("env", None)
            safe["enabled"] = False
            target = str(name)
            if target in current_servers and current_servers[target] != safe:
                target = f"{name}-import-{archive_id[:8]}"
                report["remapped"] += 1
            server_mappings[str(name)] = target
            if target in current_servers and current_servers[target] == safe:
                report["skipped"] += 1
                continue
            current_servers[target] = safe
            report["imported"] += 1
        if imported_servers:
            _write_json(self.layout.project_mcp, current)
            import_id = uuid.uuid5(uuid.NAMESPACE_URL, f"capslock:{archive_id}").hex
            connection = sqlite3.connect(self.layout.database)
            try:
                rows = connection.execute(
                    """SELECT id,request_json FROM actions WHERE import_id=? AND
                       status='pending' AND action_type IN ('mcp_connect','mcp_call')""",
                    (import_id,),
                ).fetchall()
                for action_id, encoded in rows:
                    request = json.loads(encoded)
                    server = request.get("server")
                    if server in server_mappings:
                        request["server"] = server_mappings[server]
                        connection.execute(
                            "UPDATE actions SET request_json=? WHERE id=?",
                            (json.dumps(request, ensure_ascii=False), action_id),
                        )
                connection.commit()
            finally:
                connection.close()

    def _persist_import_report(self, archive_id: str, report: dict[str, Any]) -> None:
        for path in (self.layout.database, self.memory_path):
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE lifecycle_imports SET report_json=? WHERE archive_id=?",
                    (json.dumps(report, ensure_ascii=False), archive_id),
                )
                connection.commit()
            finally:
                connection.close()

    def _import_legacy_session(self, path: Path) -> dict[str, Any]:
        document = _read_json(path)
        if (
            document.get("format") != "capslock-session-export"
            or document.get("version") != 2
        ):
            raise LifecycleError("only session export version 2 is supported")
        tables = {table: document.get(table, []) for table in WORKSPACE_TABLES}
        archive_id = hashlib.sha256(path.read_bytes()).hexdigest()[:32]
        return self._merge(
            archive_id,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "1.8.0",
            tables,
            {},
        )

    def _import_legacy_memory(self, path: Path) -> dict[str, Any]:
        document = _read_json(path)
        if (
            document.get("format") != "capslock-memory-export"
            or document.get("version") != 3
        ):
            raise LifecycleError("only memory export version 3 is supported")
        now = utc_now()
        memories, revisions = [], []
        target_key = workspace_key(self.workspace)
        for index, record in enumerate(document.get("records", [])):
            identifier = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}:{index}",
            ).hex
            memories.append(
                {
                    "id": identifier,
                    "scope": "workspace",
                    "workspace_key": target_key,
                    "session_id": None,
                    "status": "active",
                    "current_revision": 1,
                    "origin": "imported",
                    "source_valid": 1,
                    "created_at": now,
                    "updated_at": now,
                    "purged_at": None,
                }
            )
            revisions.append(
                {
                    "memory_id": identifier,
                    "revision": 1,
                    "operation": "import",
                    "content": record.get("content", ""),
                    "memory_type": record.get("type", "note"),
                    "source_kind": "import",
                    "source_ref": None,
                    "confidence": record.get("confidence", 1),
                    "expires_at": record.get("expires_at"),
                    "created_at": now,
                }
            )
        archive_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        return self._merge(
            archive_hash[:32],
            archive_hash,
            "1.8.0",
            {},
            {"memories": memories, "memory_revisions": revisions},
        )


class LifecycleService:
    """Stable synchronous facade over backup and portable archive services."""

    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout
        self.workspace = layout.workspace
        self.memory_path = layout.user.memory
        self.io = LifecycleIO(layout)
        self.backup = BackupService(layout, self.memory_path, self.io)
        self.portable = PortableArchiveService(layout, self.backup, self.io)

    @property
    def backup_directory(self) -> Path:
        return self.backup.directory

    def backup_create(self, destination: Path | None = None) -> Path:
        return self.backup.create(destination)

    def backup_list(self) -> list[Path]:
        return self.backup.list()

    def backup_restore(self, archive: Path) -> Path:
        return self.backup.restore(archive)

    def verify(
        self, archive: Path, *, expected_format: str | None = None
    ) -> dict[str, Any]:
        return verify_archive(
            archive,
            supported_versions=SUPPORTED_ARCHIVE_VERSIONS,
            expected_format=expected_format,
        )

    def export(self, destination: Path, *, include_global_memory: bool = False) -> Path:
        return self.portable.export(
            destination,
            include_global_memory=include_global_memory,
        )

    def import_archive(self, archive: Path) -> dict[str, Any]:
        return self.portable.import_archive(archive)


def _database_rows(
    path: Path, tables: tuple[str, ...]
) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        return {table: [] for table in tables}
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    finally:
        connection.close()


def _select_in(
    connection: sqlite3.Connection, table: str, field: str, values: set[str]
) -> list[dict[str, Any]]:
    if not values:
        return []
    marks = ",".join("?" for _ in values)
    return [
        dict(row)
        for row in connection.execute(
            f"SELECT * FROM {table} WHERE {field} IN ({marks})", tuple(values)
        )
    ]
