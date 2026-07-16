"""User-level memory database, full-text index, history, and audit metadata."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain import MemoryInfo, MemoryScope, MemoryStatus, MemoryType


MEMORY_SCHEMA_VERSION = 1

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, content TEXT, memory_type TEXT NOT NULL, scope TEXT NOT NULL,
  workspace_key TEXT, session_id TEXT, source_kind TEXT NOT NULL, source_ref TEXT,
  confidence REAL NOT NULL, expires_at TEXT, revision INTEGER NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, purged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, workspace_key, session_id, status);
CREATE TABLE IF NOT EXISTS memory_history (
  id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL, operation TEXT NOT NULL,
  content TEXT, memory_type TEXT, source_kind TEXT, source_ref TEXT, confidence REAL,
  expires_at TEXT, status TEXT, revision INTEGER, created_at TEXT NOT NULL, undone_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_history_id ON memory_history(memory_id, id);
CREATE TABLE IF NOT EXISTS memory_accesses (
  id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
  workspace_key TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
  accessed_at TEXT NOT NULL, UNIQUE(memory_id, revision, workspace_key, session_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_access_context ON memory_accesses(workspace_key, session_id, run_id);
CREATE TABLE IF NOT EXISTS memory_audit (
  id INTEGER PRIMARY KEY, memory_id TEXT, operation TEXT NOT NULL, scope TEXT,
  workspace_key TEXT, session_id TEXT, revision INTEGER, detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_workspace_settings (
  workspace_key TEXT PRIMARY KEY, write_enabled INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, content, tokenize='unicode61');
"""


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def workspace_key(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()


class MemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA secure_delete = ON")
        try:
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    @property
    def fts_available(self) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone() is not None

    def _migrate(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > MEMORY_SCHEMA_VERSION:
            raise RuntimeError(
                f"memory database schema {version} is newer than supported schema {MEMORY_SCHEMA_VERSION}"
            )
        tables = {
            str(row[0])
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        backup: Path | None = None
        if version < MEMORY_SCHEMA_VERSION and tables:
            backup = self._backup(version)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for statement in MEMORY_SCHEMA.split(";"):
                if statement.strip():
                    self.connection.execute(statement)
            self.connection.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
            self.connection.commit()
        except Exception as exc:
            self.connection.rollback()
            message = "memory database migration failed"
            if backup is not None:
                message += f"; backup retained at {backup}"
            raise RuntimeError(message) from exc

    def _backup(self, version: int) -> Path:
        directory = self.path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target_path = directory / f"{self.path.stem}-schema-v{version}-{stamp}{self.path.suffix}"
        suffix = 1
        while target_path.exists():
            target_path = directory / f"{self.path.stem}-schema-v{version}-{stamp}-{suffix}{self.path.suffix}"
            suffix += 1
        target = sqlite3.connect(target_path)
        try:
            self.connection.backup(target)
        finally:
            target.close()
        target_path.chmod(0o600)
        return target_path

    def create(
        self,
        *,
        content: str,
        memory_type: MemoryType,
        scope: MemoryScope,
        workspace: str | None,
        session_id: str | None,
        source_kind: str,
        source_ref: str | None,
        confidence: float,
        expires_at: str | None,
    ) -> MemoryInfo:
        with self.connection:
            item = self._insert(
                self.connection,
                content=content,
                memory_type=memory_type,
                scope=scope,
                workspace=workspace,
                session_id=session_id,
                source_kind=source_kind,
                source_ref=source_ref,
                confidence=confidence,
                expires_at=expires_at,
            )
            self._history(self.connection, item, "create", empty=True)
            self._audit(self.connection, item, "add")
        return item

    def import_many(self, records: list[dict[str, Any]]) -> list[MemoryInfo]:
        output: list[MemoryInfo] = []
        with self.connection:
            for record in records:
                item = self._insert(self.connection, **record)
                self._history(self.connection, item, "create", empty=True)
                self._audit(self.connection, item, "import")
                output.append(item)
        return output

    def _insert(
        self,
        connection: sqlite3.Connection,
        *,
        content: str,
        memory_type: MemoryType,
        scope: MemoryScope,
        workspace: str | None,
        session_id: str | None,
        source_kind: str,
        source_ref: str | None,
        confidence: float,
        expires_at: str | None,
    ) -> MemoryInfo:
        identifier, created = f"mem_{uuid.uuid4().hex}", timestamp()
        connection.execute(
            """INSERT INTO memories(
              id,content,memory_type,scope,workspace_key,session_id,source_kind,source_ref,
              confidence,expires_at,revision,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,1,'active',?,?)""",
            (
                identifier, content, memory_type.value, scope.value, workspace, session_id,
                source_kind, source_ref, confidence, expires_at, created, created,
            ),
        )
        connection.execute("INSERT INTO memory_fts(memory_id,content) VALUES(?,?)", (identifier, content))
        return self.get(identifier, include_inactive=True)  # type: ignore[return-value]

    def edit(
        self,
        memory_id: str,
        *,
        content: str,
        memory_type: MemoryType,
        source_kind: str,
        source_ref: str | None,
        confidence: float,
        expires_at: str | None,
    ) -> MemoryInfo:
        current = self._required(memory_id)
        if current.status is not MemoryStatus.ACTIVE:
            raise ValueError("only active memories can be edited")
        with self.connection:
            self._history(self.connection, current, "edit")
            self.connection.execute(
                """UPDATE memories SET content=?,memory_type=?,source_kind=?,source_ref=?,
                   confidence=?,expires_at=?,revision=revision+1,updated_at=? WHERE id=?""",
                (content, memory_type.value, source_kind, source_ref, confidence, expires_at, timestamp(), current.id),
            )
            self._replace_fts(self.connection, current.id, content)
            updated = self._required(current.id)
            self._audit(self.connection, updated, "edit")
        return updated

    def forget(self, memory_id: str) -> MemoryInfo:
        current = self._required(memory_id)
        if current.status is not MemoryStatus.ACTIVE:
            raise ValueError("only active memories can be forgotten")
        with self.connection:
            self._history(self.connection, current, "forget")
            self.connection.execute(
                "UPDATE memories SET status='forgotten',revision=revision+1,updated_at=? WHERE id=?",
                (timestamp(), current.id),
            )
            self.connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (current.id,))
            updated = self._required(current.id)
            self._audit(self.connection, updated, "forget")
        return updated

    def undo(self, memory_id: str) -> MemoryInfo:
        current = self._required(memory_id)
        history = self.connection.execute(
            "SELECT * FROM memory_history WHERE memory_id=? AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
            (current.id,),
        ).fetchone()
        if history is None:
            raise ValueError("no reversible memory operation is available")
        with self.connection:
            if history["operation"] == "create":
                self.connection.execute(
                    "UPDATE memories SET status='forgotten',revision=revision+1,updated_at=? WHERE id=?",
                    (timestamp(), current.id),
                )
                self.connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (current.id,))
            else:
                self.connection.execute(
                    """UPDATE memories SET content=?,memory_type=?,source_kind=?,source_ref=?,confidence=?,
                       expires_at=?,status=?,revision=revision+1,updated_at=? WHERE id=?""",
                    (
                        history["content"], history["memory_type"], history["source_kind"],
                        history["source_ref"], history["confidence"], history["expires_at"],
                        history["status"], timestamp(), current.id,
                    ),
                )
                self.connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (current.id,))
                if history["status"] == MemoryStatus.ACTIVE.value and history["content"] is not None:
                    self.connection.execute(
                        "INSERT INTO memory_fts(memory_id,content) VALUES(?,?)",
                        (current.id, history["content"]),
                    )
            self.connection.execute("UPDATE memory_history SET undone_at=? WHERE id=?", (timestamp(), history["id"]))
            updated = self._required(current.id)
            self._audit(self.connection, updated, "undo", detail=str(history["operation"]))
        return updated

    def purge(self, memory_id: str) -> MemoryInfo:
        current = self._required(memory_id)
        if current.status is MemoryStatus.PURGED:
            raise ValueError("memory is already purged")
        purged = timestamp()
        with self.connection:
            self.connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (current.id,))
            self.connection.execute("DELETE FROM memory_history WHERE memory_id=?", (current.id,))
            self.connection.execute(
                """UPDATE memories SET content=NULL,source_ref=NULL,confidence=0,expires_at=NULL,
                   status='purged',revision=revision+1,updated_at=?,purged_at=? WHERE id=?""",
                (purged, purged, current.id),
            )
            updated = self._required(current.id)
            self._audit(self.connection, updated, "purge")
        return updated

    def get(self, memory_id: str, *, include_inactive: bool = False) -> MemoryInfo | None:
        row = self.connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        item = None if row is None else _memory_info(row)
        if item is None or include_inactive:
            return item
        return item if _active(item) else None

    def resolve(
        self,
        prefix: str,
        *,
        workspace: str,
        session_id: str,
        include_inactive: bool = True,
    ) -> MemoryInfo:
        matches = [
            item for item in self.list_visible(
                workspace=workspace, session_id=session_id, include_inactive=include_inactive, limit=10_000
            ) if item.id == prefix or item.id.startswith(prefix)
        ]
        exact = next((item for item in matches if item.id == prefix), None)
        if exact is not None:
            return exact
        if len(matches) > 1:
            raise ValueError("memory id prefix is ambiguous; provide more characters")
        if not matches:
            raise ValueError("memory does not exist or is outside the current scope")
        return matches[0]

    def list_visible(
        self,
        *,
        workspace: str,
        session_id: str,
        scope: MemoryScope | None = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> list[MemoryInfo]:
        where, values = self._visible_where(workspace, session_id)
        if scope is not None:
            where += " AND scope=?"
            values.append(scope.value)
        if not include_inactive:
            where += " AND status='active' AND (expires_at IS NULL OR expires_at>?)"
            values.append(timestamp())
        rows = self.connection.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY updated_at DESC LIMIT ?", (*values, limit)
        ).fetchall()
        return [_memory_info(row) for row in rows]

    def search(self, query: str, *, workspace: str, session_id: str, limit: int = 10) -> list[MemoryInfo]:
        where, values = self._visible_where(workspace, session_id, alias="m")
        active = "m.status='active' AND (m.expires_at IS NULL OR m.expires_at>?)"
        now = timestamp()
        phrase = '"' + query.replace('"', '""') + '"'
        rows: list[sqlite3.Row] = []
        try:
            rows = self.connection.execute(
                f"""SELECT m.*,bm25(memory_fts) AS rank FROM memory_fts
                    JOIN memories m ON m.id=memory_fts.memory_id
                    WHERE memory_fts MATCH ? AND {where} AND {active}
                    ORDER BY rank,m.updated_at DESC LIMIT ?""",
                (phrase, *values, now, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        seen = {str(row["id"]) for row in rows}
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        fallback = self.connection.execute(
            f"""SELECT m.* FROM memories m WHERE {where} AND {active}
                AND m.content LIKE ? ESCAPE '\\' ORDER BY m.updated_at DESC LIMIT ?""",
            (*values, now, f"%{escaped}%", limit),
        ).fetchall()
        rows.extend(row for row in fallback if str(row["id"]) not in seen)
        return [_memory_info(row) for row in rows[:limit]]

    def record_access(self, memories: list[MemoryInfo], *, workspace: str, session_id: str, run_id: str) -> None:
        with self.connection:
            self.connection.executemany(
                """INSERT OR IGNORE INTO memory_accesses(
                   memory_id,revision,workspace_key,session_id,run_id,accessed_at
                   ) VALUES(?,?,?,?,?,?)""",
                [(item.id, item.revision, workspace, session_id, run_id, timestamp()) for item in memories],
            )

    def excluded_runs(self, *, workspace: str, session_id: str) -> set[str]:
        rows = self.connection.execute(
            """SELECT DISTINCT a.run_id FROM memory_accesses a
               LEFT JOIN memories m ON m.id=a.memory_id
               WHERE a.workspace_key=? AND a.session_id=? AND (
                 m.id IS NULL OR m.status!='active' OR m.expires_at<=? OR m.revision!=a.revision
               )""",
            (workspace, session_id, timestamp()),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def local_write_enabled(self, workspace: str) -> bool:
        row = self.connection.execute(
            "SELECT write_enabled FROM memory_workspace_settings WHERE workspace_key=?", (workspace,)
        ).fetchone()
        return True if row is None else bool(row[0])

    def set_local_write_enabled(self, workspace: str, enabled: bool) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO memory_workspace_settings(workspace_key,write_enabled) VALUES(?,?)
                   ON CONFLICT(workspace_key) DO UPDATE SET write_enabled=excluded.write_enabled""",
                (workspace, int(enabled)),
            )
            self.connection.execute(
                """INSERT INTO memory_audit(operation,workspace_key,detail,created_at)
                   VALUES('policy',?,?,?)""",
                (workspace, "enabled" if enabled else "disabled", timestamp()),
            )

    def audit_export(self, *, workspace: str, session_id: str, scope: MemoryScope, count: int) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO memory_audit(operation,scope,workspace_key,session_id,detail,created_at)
                   VALUES('export',?,?,?,?,?)""",
                (scope.value, workspace, session_id, f"count={count}", timestamp()),
            )

    def _visible_where(self, workspace: str, session_id: str, *, alias: str = "") -> tuple[str, list[Any]]:
        prefix = f"{alias}." if alias else ""
        return (
            f"({prefix}scope='global' OR ({prefix}scope='workspace' AND {prefix}workspace_key=?) "
            f"OR ({prefix}scope='session' AND {prefix}workspace_key=? AND {prefix}session_id=?))",
            [workspace, workspace, session_id],
        )

    def _required(self, memory_id: str) -> MemoryInfo:
        item = self.get(memory_id, include_inactive=True)
        if item is None:
            raise ValueError("memory does not exist")
        return item

    def _replace_fts(self, connection: sqlite3.Connection, memory_id: str, content: str) -> None:
        connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        connection.execute("INSERT INTO memory_fts(memory_id,content) VALUES(?,?)", (memory_id, content))

    def _history(self, connection: sqlite3.Connection, item: MemoryInfo, operation: str, *, empty: bool = False) -> None:
        connection.execute(
            """INSERT INTO memory_history(
              memory_id,operation,content,memory_type,source_kind,source_ref,confidence,
              expires_at,status,revision,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.id, operation, None if empty else item.content, None if empty else item.type.value,
                None if empty else item.source_kind, None if empty else item.source_ref,
                None if empty else item.confidence, None if empty else item.expires_at,
                None if empty else item.status.value, item.revision, timestamp(),
            ),
        )

    def _audit(self, connection: sqlite3.Connection, item: MemoryInfo, operation: str, *, detail: str | None = None) -> None:
        connection.execute(
            """INSERT INTO memory_audit(
              memory_id,operation,scope,workspace_key,session_id,revision,detail,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (item.id, operation, item.scope.value, item.workspace_key, item.session_id, item.revision, detail, timestamp()),
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _active(item: MemoryInfo) -> bool:
    return item.status is MemoryStatus.ACTIVE and (item.expires_at is None or item.expires_at > timestamp())


def _memory_info(row: sqlite3.Row) -> MemoryInfo:
    return MemoryInfo(
        id=str(row["id"]), content=row["content"], type=MemoryType(row["memory_type"]),
        scope=MemoryScope(row["scope"]), workspace_key=row["workspace_key"], session_id=row["session_id"],
        source_kind=str(row["source_kind"]), source_ref=row["source_ref"], confidence=float(row["confidence"]),
        expires_at=row["expires_at"], revision=int(row["revision"]), status=MemoryStatus(row["status"]),
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]), purged_at=row["purged_at"],
    )
