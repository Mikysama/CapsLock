"""Lifecycle service orchestration and compatibility facade implementation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .. import __version__
from ..layout import ProjectLayout
from ..storage.memory_v2 import workspace_key
from .archive import (
    MAX_ARCHIVE_BYTES,
    extract_zip as _extract_zip,
    read_json as _read_json,
    validate_zip as _validate_zip,
    write_json as _write_json,
    write_zip as _write_zip,
)
from .errors import LifecycleError
from .coordinator import ImportCoordinator
from .sanitization import (
    redact_portable as _redact_portable,
    sanitize_config as _sanitize_config,
    sanitize_mcp as _sanitize_mcp,
)
from .specs import (
    MEMORY_TABLES,
    REFERENCE_FIELDS,
    WORKSPACE_TABLES,
)


BACKUP_FORMAT = "capslock-backup"
EXPORT_FORMAT = "capslock-lifecycle-export"
ARCHIVE_VERSION = 2
SUPPORTED_ARCHIVE_VERSIONS = frozenset({1, ARCHIVE_VERSION})
MAX_ARCHIVE_RECORDS = 100_000


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LifecycleService:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout
        self.workspace = layout.workspace
        self.memory_path = layout.user.memory

    @property
    def backup_directory(self) -> Path:
        identity = workspace_key(self.workspace)[:16]
        return self.layout.user.home / "backups" / identity

    def backup_create(self, destination: Path | None = None) -> Path:
        self.backup_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = destination or self.backup_directory / f"capslock-{stamp}.clbackup"
        target = target.expanduser().resolve()
        if target.exists():
            raise FileExistsError(f"backup already exists: {target}")
        with (
            self._locks(),
            tempfile.TemporaryDirectory(prefix="capslock-backup-") as raw,
        ):
            stage = Path(raw)
            missing: list[str] = []
            self._snapshot_database(
                self.layout.database, stage / "workspace.sqlite3", missing
            )
            self._snapshot_database(self.memory_path, stage / "memory.sqlite3", missing)
            for source, name in (
                (self.layout.config, "config.toml"),
                (self.layout.project_mcp, "mcp.json"),
                (self.layout.local_mcp, "local-mcp.json"),
                (self.layout.events, "events.jsonl"),
            ):
                if not source.is_file():
                    continue
                if name == "config.toml":
                    if _sanitize_config(source, stage / name):
                        missing.append("plaintext configuration credentials")
                elif name == "local-mcp.json":
                    _write_json(stage / name, _sanitize_mcp(_read_json(source)))
                    missing.append("MCP environment values")
                else:
                    shutil.copy2(source, stage / name)
            _copy_tree(self.layout.skills, stage / "project-skills")
            _copy_tree(self.layout.user.skills, stage / "user-skills")
            manifest = self._manifest(
                BACKUP_FORMAT, stage, extra={"missing_secrets": sorted(set(missing))}
            )
            _write_json(stage / "manifest.json", manifest)
            _write_zip(stage, target)
        target.chmod(0o600)
        return target

    def backup_list(self) -> list[Path]:
        if not self.backup_directory.is_dir():
            return []
        return sorted(self.backup_directory.glob("*.clbackup"), reverse=True)

    def verify(
        self, archive: Path, *, expected_format: str | None = None
    ) -> dict[str, Any]:
        archive = archive.expanduser().resolve()
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise LifecycleError("archive exceeds the size limit")
        with zipfile.ZipFile(archive) as bundle:
            _validate_zip(bundle)
            try:
                manifest = json.loads(bundle.read("manifest.json"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LifecycleError("archive has no valid manifest") from exc
            if manifest.get("version") not in SUPPORTED_ARCHIVE_VERSIONS:
                raise LifecycleError("unsupported archive version")
            if expected_format and manifest.get("format") != expected_format:
                raise LifecycleError(f"expected {expected_format} archive")
            expected = manifest.get("files")
            if not isinstance(expected, dict):
                raise LifecycleError("archive manifest has no file checksums")
            actual_names = {item.filename for item in bundle.infolist()} - {
                "manifest.json"
            }
            if actual_names != set(expected):
                raise LifecycleError("archive file list does not match its manifest")
            for name, digest in expected.items():
                if hashlib.sha256(bundle.read(name)).hexdigest() != digest:
                    raise LifecycleError(f"archive checksum mismatch: {name}")
            return manifest

    def backup_restore(self, archive: Path) -> Path:
        self.verify(archive, expected_format=BACKUP_FORMAT)
        safety = self.backup_create()
        journal = self._journal("restore", str(archive), str(safety))
        try:
            with (
                self._locks(),
                tempfile.TemporaryDirectory(prefix="capslock-restore-") as raw,
            ):
                stage = Path(raw)
                _extract_zip(archive, stage)
                replacements = (
                    (stage / "workspace.sqlite3", self.layout.database),
                    (stage / "memory.sqlite3", self.memory_path),
                    (stage / "config.toml", self.layout.config),
                    (stage / "mcp.json", self.layout.project_mcp),
                    (stage / "local-mcp.json", self.layout.local_mcp),
                    (stage / "events.jsonl", self.layout.events),
                )
                for source, destination in replacements:
                    if source.exists():
                        _atomic_replace(source, destination)
                    elif destination.exists():
                        destination.unlink()
                for source, destination in (
                    (stage / "project-skills", self.layout.skills),
                    (stage / "user-skills", self.layout.user.skills),
                ):
                    if source.is_dir():
                        _replace_tree(source, destination)
                    elif destination.exists():
                        shutil.rmtree(destination)
            journal.unlink(missing_ok=True)
            return safety
        except Exception:
            raise LifecycleError(
                f"restore failed; recovery backup: {safety}; journal: {journal}"
            )

    def export(self, destination: Path, *, include_global_memory: bool = False) -> Path:
        target = destination.expanduser().resolve()
        if target.exists():
            raise FileExistsError(f"export already exists: {target}")
        with (
            self._locks(),
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
            manifest = self._manifest(
                EXPORT_FORMAT,
                stage,
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
        manifest = self.verify(archive, expected_format=EXPORT_FORMAT)
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
        return ImportCoordinator(self).merge(
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

    def _manifest(
        self, archive_format: str, stage: Path, *, extra: dict[str, Any]
    ) -> dict[str, Any]:
        files = {
            path.relative_to(stage).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(stage.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        }
        return {
            "format": archive_format,
            "version": ARCHIVE_VERSION,
            "source_version": __version__,
            "created_at": utc_now(),
            "workspace_fingerprint": workspace_key(self.workspace),
            "files": files,
            **extra,
        }

    def _snapshot_database(
        self, source: Path, target: Path, missing: list[str]
    ) -> None:
        if source.is_file():
            _sqlite_backup(source, target)
        else:
            missing.append(str(source))

    def _journal(self, operation: str, source: str, recovery: str) -> Path:
        path = self.layout.root / "state" / "lifecycle-journal.json"
        _write_json(
            path,
            {
                "operation": operation,
                "source": source,
                "recovery": recovery,
                "started_at": utc_now(),
            },
        )
        path.chmod(0o600)
        return path

    @contextmanager
    def _locks(self) -> Iterator[None]:
        import fcntl

        paths = sorted(
            (
                self.layout.root / "state" / "lifecycle.lock",
                self.layout.user.home / "state" / "lifecycle.lock",
            )
        )
        with ExitStack() as stack:
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = stack.enter_context(path.open("a+"))
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

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


def _merge_tables(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
    tables: tuple[str, ...],
    primary: dict[str, str | tuple[str, ...]],
    import_id: str,
    archive_id: str,
    report: dict[str, Any],
    *,
    domain: str,
    external_maps: dict[str, dict[str, str]] | None = None,
    target_workspace_key: str | None = None,
) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {}
    all_maps = external_maps or {}
    for table in tables:
        records = payload.get(table, [])
        if not isinstance(records, list):
            raise LifecycleError(f"{table} must be a list")
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not columns:
            continue
        key_spec = primary[table]
        keys = (key_spec,) if isinstance(key_spec, str) else key_spec
        table_map: dict[str, str] = {}
        for source in records:
            if not isinstance(source, dict) or any(key not in source for key in keys):
                raise LifecycleError(f"invalid {table} record")
            record = {name: value for name, value in source.items() if name in columns}
            source_id = json.dumps(
                [source[key] for key in keys], separators=(",", ":"), default=str
            )
            _rewrite_references(
                table, record, {**all_maps, **maps}, target_workspace_key
            )
            fingerprint = _fingerprint(record)
            target_values = tuple(record[key] for key in keys)
            where = " AND ".join(f"{key}=?" for key in keys)
            existing = connection.execute(
                f"SELECT * FROM {table} WHERE {where}", target_values
            ).fetchone()
            disposition = "imported"
            if existing is not None:
                existing_value = {name: existing[name] for name in record}
                if _fingerprint(existing_value) == fingerprint:
                    disposition = "skipped"
                elif len(keys) > 1 or table in {
                    "workspace_settings",
                    "skill_settings",
                    "memory_workspace_settings",
                }:
                    disposition = "blocked"
                else:
                    key = keys[0]
                    target_values = (
                        _remapped_id(
                            connection,
                            table,
                            key,
                            archive_id,
                            source_id,
                            target_values[0],
                        ),
                    )
                    record[key] = target_values[0]
                    disposition = "remapped"
            target_id = json.dumps(target_values, separators=(",", ":"), default=str)
            if len(keys) == 1:
                table_map[str(source[keys[0]])] = str(target_values[0])
                target_id = str(target_values[0])
            if disposition not in {"skipped", "blocked"}:
                if table == "actions":
                    record["import_id"] = import_id
                _insert_record(connection, table, record)
            report[disposition] += 1
            connection.execute(
                """INSERT OR REPLACE INTO lifecycle_import_items(
                   import_id,entity_type,source_id,target_id,fingerprint,disposition) VALUES(?,?,?,?,?,?)""",
                (
                    import_id,
                    f"{domain}:{table}",
                    source_id,
                    target_id,
                    fingerprint,
                    disposition,
                ),
            )
        maps[table] = table_map
    return maps


def _rewrite_references(
    table_name: str,
    record: dict[str, Any],
    maps: dict[str, dict[str, str]],
    target_workspace_key: str | None,
) -> None:
    references = dict(REFERENCE_FIELDS)
    if table_name in {"memory_recalls", "memory_recall_items"}:
        references["run_id"] = (
            "memory_recalls" if table_name == "memory_recall_items" else "runs"
        )
    for field, table in references.items():
        value = record.get(field)
        if value is not None and str(value) in maps.get(table, {}):
            record[field] = maps[table][str(value)]
    if (
        target_workspace_key
        and "workspace_key" in record
        and not (table_name == "memories" and record.get("scope") == "global")
    ):
        record["workspace_key"] = target_workspace_key


def _normalize_imported_workflow(
    connection: sqlite3.Connection, import_id: str
) -> None:
    def imported(table: str) -> str:
        return f"id IN (SELECT target_id FROM lifecycle_import_items WHERE import_id=? AND entity_type='workspace:{table}' AND disposition IN ('imported','remapped'))"

    connection.execute(
        f"UPDATE work_items SET status='interrupted',error='interrupted during export' WHERE status='running' AND {imported('work_items')}",
        (import_id,),
    )
    connection.execute(
        f"UPDATE runs SET status='interrupted',finished_at=coalesce(finished_at,?),error_code='imported_interrupted',error_message='interrupted during export' WHERE status='running' AND {imported('runs')}",
        (utc_now(), import_id),
    )
    connection.execute(
        f"""INSERT OR IGNORE INTO run_governance(
               run_id,root_run_id,mode,limits_json,tool_rounds,tool_calls,
               elapsed_ms,input_tokens,output_tokens,cost_usd,updated_at)
            SELECT r.id,r.id,'interactive',
                   '{{"max_tool_rounds":32,"max_tool_calls":null,"max_duration_seconds":null,"max_tokens":null,"max_budget_usd":null}}',
                   (SELECT count(*) FROM run_steps s WHERE s.run_id=r.id AND s.kind='model' AND s.status='completed' AND instr(coalesce(s.checkpoint_json,''),'tool_calls')>0),
                   (SELECT count(*) FROM tool_calls t WHERE t.run_id=r.id),
                   coalesce(r.duration_ms,0),r.input_tokens,r.output_tokens,r.cost_usd,?
              FROM runs r WHERE {imported("runs")}""",
        (utc_now(), import_id),
    )
    connection.execute(
        f"UPDATE run_steps SET status='cancelled',finished_at=coalesce(finished_at,?),error='interrupted during export' WHERE status='running' AND {imported('run_steps')}",
        (utc_now(), import_id),
    )
    connection.execute(
        f"UPDATE model_calls SET status='failed',finished_at=coalesce(finished_at,?),error_code='imported_interrupted',error_message='interrupted during export' WHERE status='running' AND {imported('model_calls')}",
        (utc_now(), import_id),
    )
    connection.execute(
        f"UPDATE agent_tasks SET state='interrupted',finished_at=coalesce(finished_at,?),error='interrupted during export',child_workspace=NULL WHERE state IN ('created','running','waiting_approval') AND {imported('agent_tasks')}",
        (utc_now(), import_id),
    )
    connection.execute(
        """UPDATE actions SET historical_only=1,requires_reapproval=0
                          WHERE import_id=? AND status IN ('completed','failed','rejected','cancelled')""",
        (import_id,),
    )
    connection.execute(
        """UPDATE actions SET status='pending',approved_at=NULL,started_at=NULL,
                          finished_at=NULL,decided_at=NULL,result_json=NULL,result_kind=NULL,
                          error_code=NULL,error_message=NULL,historical_only=0,requires_reapproval=1
                          WHERE import_id=? AND status IN ('pending','approved','running')""",
        (import_id,),
    )


def _rebuild_session_search(
    connection: sqlite3.Connection, session_ids: set[str]
) -> None:
    for session_id in session_ids:
        connection.execute(
            "DELETE FROM session_search WHERE session_id=?", (session_id,)
        )
        session = connection.execute(
            "SELECT title,created_at FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if session is None:
            continue
        connection.execute(
            "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
            (session_id, "title", session["title"], session["created_at"]),
        )
        connection.executemany(
            "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
            [
                (session_id, "message", row["content"], row["created_at"])
                for row in connection.execute(
                    "SELECT content,created_at FROM messages WHERE session_id=? ORDER BY id",
                    (session_id,),
                )
            ],
        )


def _rebuild_memory_fts(connection: sqlite3.Connection, memory_ids: set[str]) -> None:
    for memory_id in memory_ids:
        connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        row = connection.execute(
            """SELECT m.current_revision,r.content FROM memories m
               LEFT JOIN memory_revisions r ON r.memory_id=m.id AND r.revision=m.current_revision
               WHERE m.id=? AND m.status='active'""",
            (memory_id,),
        ).fetchone()
        if row is not None and row[0] is not None and row[1] is not None:
            connection.execute(
                "INSERT INTO memory_fts(memory_id,revision,content) VALUES(?,?,?)",
                (memory_id, row[0], row[1]),
            )


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


def _insert_record(
    connection: sqlite3.Connection, table: str, record: dict[str, Any]
) -> None:
    columns = tuple(record)
    marks = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({marks})",
        tuple(record[name] for name in columns),
    )


def _remapped_id(
    connection: sqlite3.Connection,
    table: str,
    key: str,
    archive_id: str,
    source_id: str,
    current: object,
) -> object:
    integer = isinstance(current, int)
    for attempt in range(1000):
        seed = f"{archive_id}:{table}:{source_id}:{attempt}"
        candidate: object = (
            int(hashlib.sha256(seed.encode()).hexdigest()[:14], 16)
            if integer
            else uuid.uuid5(uuid.NAMESPACE_URL, seed).hex
        )
        if (
            connection.execute(
                f"SELECT 1 FROM {table} WHERE {key}=?", (candidate,)
            ).fetchone()
            is None
        ):
            return candidate
    raise LifecycleError(f"could not remap {table} id")


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    original = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        original.backup(copy)
    finally:
        original.close()
        copy.close()
    target.chmod(0o600)


def _copy_tree(source: Path, target: Path) -> None:
    if source.is_dir():
        symlink = next((path for path in source.rglob("*") if path.is_symlink()), None)
        if symlink is not None:
            raise LifecycleError(f"backup does not follow symbolic links: {symlink}")
        shutil.copytree(source, target, symlinks=False, ignore_dangling_symlinks=True)


def _replace_tree(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, temporary)
    if target.exists():
        shutil.rmtree(target)
    os.replace(temporary, target)


def _atomic_replace(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)
