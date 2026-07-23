"""Cross-database portable import recovery coordinator."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from ..storage.memory_repositories import workspace_key
from .errors import LifecycleError
from .import_merge import (
    merge_tables,
    normalize_imported_workflow,
    rebuild_memory_fts,
    rebuild_session_search,
    utc_now,
)
from .io import sqlite_backup
from .specs import MEMORY_PRIMARY, MEMORY_TABLES, WORKSPACE_PRIMARY, WORKSPACE_TABLES


class ImportCoordinator:
    """Own locks, recovery snapshots, journals, and two-database commit order."""

    def __init__(
        self,
        *,
        layout: Any,
        memory_path: Path,
        workspace: Path,
        backup_create: Callable[[], Path],
        locks: Callable[[], AbstractContextManager[None]],
        journal: Callable[[str, str, str], Path],
    ) -> None:
        self.layout = layout
        self.memory_path = memory_path
        self.workspace = workspace
        self.backup_create = backup_create
        self.locks = locks
        self.journal = journal

    def merge(
        self,
        archive_id: str,
        archive_hash: str,
        source_version: str,
        workspace_rows: dict[str, Any],
        memory_rows: dict[str, Any],
    ) -> dict[str, Any]:
        layout, memory_path = self.layout, self.memory_path
        if not layout.database.exists() or not memory_path.exists():
            raise LifecycleError("initialize both databases before importing")
        report: dict[str, Any] = {
            "archive_id": archive_id,
            "imported": 0,
            "skipped": 0,
            "remapped": 0,
            "blocked": 0,
            "mappings": {},
        }
        recovery = self.backup_create()
        journal = self.journal("import", archive_id, str(recovery))
        with (
            self.locks(),
            tempfile.TemporaryDirectory(prefix="capslock-import-rollback-") as raw,
        ):
            rollback = Path(raw)
            sqlite_backup(layout.database, rollback / "workspace.sqlite3")
            sqlite_backup(memory_path, rollback / "memory.sqlite3")
            workspace_connection = sqlite3.connect(layout.database)
            memory_connection = sqlite3.connect(memory_path)
            workspace_connection.row_factory = sqlite3.Row
            memory_connection.row_factory = sqlite3.Row
            import_id = uuid.uuid5(uuid.NAMESPACE_URL, f"capslock:{archive_id}").hex
            try:
                existing = workspace_connection.execute(
                    "SELECT status,report_json FROM lifecycle_imports WHERE archive_id=?",
                    (archive_id,),
                ).fetchone()
                if existing and existing["status"] == "completed":
                    journal.unlink(missing_ok=True)
                    return json.loads(existing["report_json"])
                for connection in (workspace_connection, memory_connection):
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """INSERT OR REPLACE INTO lifecycle_imports(
                           id,archive_id,archive_sha256,source_version,status,report_json,created_at,completed_at)
                           VALUES(?,?,?,?, 'running','{}',?,NULL)""",
                        (
                            import_id,
                            archive_id,
                            archive_hash,
                            source_version,
                            utc_now(),
                        ),
                    )
                workspace_maps = merge_tables(
                    workspace_connection,
                    workspace_rows,
                    WORKSPACE_TABLES,
                    WORKSPACE_PRIMARY,
                    import_id,
                    archive_id,
                    report,
                    domain="workspace",
                )
                normalize_imported_workflow(workspace_connection, import_id)
                rebuild_session_search(
                    workspace_connection,
                    set(workspace_maps.get("sessions", {}).values()),
                )
                memory_maps = merge_tables(
                    memory_connection,
                    memory_rows,
                    MEMORY_TABLES,
                    MEMORY_PRIMARY,
                    import_id,
                    archive_id,
                    report,
                    domain="memory",
                    external_maps=workspace_maps,
                    target_workspace_key=workspace_key(self.workspace),
                )
                rebuild_memory_fts(
                    memory_connection,
                    set(memory_maps.get("memories", {}).values()),
                )
                report["mappings"] = {**workspace_maps, **memory_maps}
                for connection in (workspace_connection, memory_connection):
                    connection.execute(
                        "UPDATE lifecycle_imports SET status='completed',report_json=?,completed_at=? WHERE id=?",
                        (json.dumps(report, ensure_ascii=False), utc_now(), import_id),
                    )
                workspace_connection.commit()
                memory_connection.commit()
            except BaseException:
                workspace_connection.rollback()
                memory_connection.rollback()
                workspace_connection.close()
                memory_connection.close()
                shutil.copy2(rollback / "workspace.sqlite3", layout.database)
                shutil.copy2(rollback / "memory.sqlite3", memory_path)
                raise
            else:
                workspace_connection.close()
                memory_connection.close()
                journal.unlink(missing_ok=True)
        return report
