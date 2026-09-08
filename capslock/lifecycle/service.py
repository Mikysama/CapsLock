"""Lifecycle service orchestration."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..layout import ProjectLayout
from ..planning import plan_mirror_path, write_plan_mirror
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
from .backup import BackupService
from .coordinator import ImportCoordinator
from .errors import LifecycleError
from .import_merge import rebuild_episodic_search
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
ARCHIVE_VERSION = 7
SUPPORTED_ARCHIVE_VERSIONS = frozenset({3, 4, 5, 6, ARCHIVE_VERSION})
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

    def export(
        self,
        destination: Path,
        *,
        include_global_memory: bool = False,
        include_artifacts: bool = False,
    ) -> Path:
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
            if include_artifacts and self.layout.artifacts.is_dir():
                shutil.copytree(self.layout.artifacts, stage / "artifacts")
            manifest = build_manifest(
                EXPORT_FORMAT,
                stage,
                workspace=self.workspace,
                version=ARCHIVE_VERSION,
                extra={
                    "archive_id": archive_id,
                    "include_global_memory": include_global_memory,
                    "include_artifacts": include_artifacts,
                },
            )
            _write_json(stage / "manifest.json", manifest)
            _write_zip(stage, target)
        target.chmod(0o600)
        return target

    def import_archive(self, archive: Path) -> dict[str, Any]:
        archive = archive.expanduser().resolve()
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
            if int(manifest["version"]) <= 6:
                for table in (
                    "agent_capabilities",
                    "context_snapshots",
                    "citations",
                ):
                    obsolete_rows = data_workspace.pop(table, None)
                    if obsolete_rows is not None and not isinstance(
                        obsolete_rows, list
                    ):
                        raise LifecycleError(
                            f"portable archive table {table} must be a list"
                        )
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
            report["plan_mirror_failures"] = self._rebuild_plan_mirrors(report)
            if (stage / "artifacts").is_dir():
                shutil.copytree(
                    stage / "artifacts", self.layout.artifacts, dirs_exist_ok=True
                )
            self._rebuild_episodic(report)
            self._merge_mcp(_read_json(stage / "mcp.json"), archive_id, report)
            self._persist_import_report(archive_id, report)
            return report

    def _rebuild_episodic(self, report: dict[str, Any]) -> None:
        mappings = report.get("mappings", {})
        session_mapping = (
            mappings.get("sessions", {}) if isinstance(mappings, dict) else {}
        )
        if not isinstance(session_mapping, dict) or not session_mapping:
            return
        connection = sqlite3.connect(self.layout.database)
        connection.row_factory = sqlite3.Row
        try:
            rebuild_episodic_search(
                connection,
                set(map(str, session_mapping.values())),
                artifact_root=self.layout.artifacts,
            )
            connection.commit()
        finally:
            connection.close()

    def _rebuild_plan_mirrors(self, report: dict[str, Any]) -> int:
        mappings = report.get("mappings", {})
        plan_mapping = (
            mappings.get("session_plans", {}) if isinstance(mappings, dict) else {}
        )
        if not isinstance(plan_mapping, dict) or not plan_mapping:
            return 0
        failures = 0
        connection = sqlite3.connect(self.layout.database)
        connection.row_factory = sqlite3.Row
        try:
            for plan_id in set(map(str, plan_mapping.values())):
                row = connection.execute(
                    """SELECT p.mirror_relative_path,v.content,v.sha256
                       FROM session_plans p JOIN plan_revisions v
                       ON v.id=p.current_revision_id AND v.plan_id=p.id
                       WHERE p.id=?""",
                    (plan_id,),
                ).fetchone()
                if row is None:
                    failures += 1
                    continue
                content = str(row["content"])
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if digest != str(row["sha256"]):
                    failures += 1
                    continue
                try:
                    target = plan_mirror_path(
                        self.layout.plans, str(row["mirror_relative_path"])
                    )
                    write_plan_mirror(target, content)
                except (OSError, ValueError, UnicodeError):
                    # The database revision remains authoritative. A later
                    # show/open/resume will retry rebuilding the mirror.
                    failures += 1
        finally:
            connection.close()
        return failures

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


class LifecycleService:
    """Synchronous facade over backup and portable archive services."""

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

    def export(
        self,
        destination: Path,
        *,
        include_global_memory: bool = False,
        include_artifacts: bool = False,
    ) -> Path:
        return self.portable.export(
            destination,
            include_global_memory=include_global_memory,
            include_artifacts=include_artifacts,
        )

    def import_archive(self, archive: Path) -> dict[str, Any]:
        return self.portable.import_archive(archive)

    def compact(self, scope: str) -> dict[str, Any]:
        """Safely VACUUM one or both managed databases."""
        if scope not in {"workspace", "memory", "all"}:
            raise ValueError("database compact scope must be workspace, memory, or all")
        selected = []
        if scope in {"workspace", "all"}:
            selected.append(("workspace", self.layout.database))
        if scope in {"memory", "all"}:
            selected.append(("memory", self.memory_path))
        selected = [(name, path) for name, path in selected if path.is_file()]
        if not selected:
            raise LifecycleError("no selected database exists")
        with self.io.locks():
            before = {name: path.stat().st_size for name, path in selected}
            for _, path in selected:
                _checkpoint_and_validate(path)
            _require_compaction_space(selected)
            backup = self.backup.create_under_lock()
            try:
                for _, path in selected:
                    connection = sqlite3.connect(path)
                    try:
                        connection.execute("PRAGMA busy_timeout=5000")
                        connection.execute("VACUUM")
                        connection.execute("PRAGMA optimize")
                        _validate_database(connection, path)
                    finally:
                        connection.close()
            except Exception as exc:
                raise LifecycleError(
                    f"database compaction failed; recovery backup: {backup}"
                ) from exc
        databases = []
        for name, path in selected:
            after = path.stat().st_size
            databases.append(
                {
                    "scope": name,
                    "path": str(path),
                    "before_bytes": before[name],
                    "after_bytes": after,
                    "reclaimed_bytes": max(0, before[name] - after),
                }
            )
        return {"backup": str(backup), "databases": databases}


def _require_compaction_space(selected: list[tuple[str, Path]]) -> None:
    by_device: dict[int, tuple[Path, int]] = {}
    for _, path in selected:
        parent = path.parent
        device = parent.stat().st_dev
        prior = by_device.get(device)
        by_device[device] = (parent, (prior[1] if prior else 0) + path.stat().st_size)
    for parent, total in by_device.values():
        required = math.ceil(total * 2.1)
        available = shutil.disk_usage(parent).free
        if available < required:
            raise LifecycleError(
                "insufficient free space for database compaction: "
                f"need {required} bytes, have {available} bytes"
            )


def _checkpoint_and_validate(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise LifecycleError(f"database WAL checkpoint is busy: {path}")
        _validate_database(connection, path)
    finally:
        connection.close()


def _validate_database(connection: sqlite3.Connection, path: Path) -> None:
    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise LifecycleError(f"database integrity check failed: {path}: {integrity[0]}")
    foreign = list(connection.execute("PRAGMA foreign_key_check"))
    if foreign:
        raise LifecycleError(f"database foreign key check failed: {path}")


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
