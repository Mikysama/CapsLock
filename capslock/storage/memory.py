"""User-level memory database, full-text index, history, and audit metadata."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain import (
    EmbeddingBackend,
    MemoryCandidateInfo,
    MemoryCandidateStatus,
    MemoryInfo,
    MemoryOrigin,
    MemoryPolicy,
    MemoryRecallHit,
    MemoryScope,
    MemoryStatus,
    MemoryType,
)


MEMORY_SCHEMA_VERSION = 2

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, content TEXT, memory_type TEXT NOT NULL, scope TEXT NOT NULL,
  workspace_key TEXT, session_id TEXT, source_kind TEXT NOT NULL, source_ref TEXT,
  confidence REAL NOT NULL, expires_at TEXT, revision INTEGER NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, purged_at TEXT,
  origin TEXT NOT NULL DEFAULT 'manual', source_valid INTEGER NOT NULL DEFAULT 1
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
  workspace_key TEXT PRIMARY KEY, write_enabled INTEGER NOT NULL,
  policy TEXT NOT NULL DEFAULT 'review', recall_enabled INTEGER NOT NULL DEFAULT 1,
  embedding_backend TEXT NOT NULL DEFAULT 'off', embedding_model TEXT,
  embedding_endpoint TEXT
);
CREATE TABLE IF NOT EXISTS memory_extractions (
  id TEXT PRIMARY KEY, workspace_key TEXT NOT NULL, session_id TEXT NOT NULL,
  source_run_id TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  policy TEXT NOT NULL, status TEXT NOT NULL, candidate_count INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  error_code TEXT, created_at TEXT NOT NULL, completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_extractions_run ON memory_extractions(workspace_key,session_id,source_run_id);
CREATE TABLE IF NOT EXISTS memory_candidates (
  id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL, content TEXT,
  memory_type TEXT NOT NULL, scope TEXT NOT NULL, workspace_key TEXT NOT NULL,
  session_id TEXT NOT NULL, source_run_id TEXT NOT NULL, confidence REAL NOT NULL,
  status TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'new', related_memory_id TEXT,
  risk_flags TEXT NOT NULL DEFAULT '[]', adopted_memory_id TEXT,
  created_at TEXT NOT NULL, decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_queue ON memory_candidates(workspace_key,session_id,status,created_at);
CREATE TABLE IF NOT EXISTS memory_sources (
  id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL, source_kind TEXT NOT NULL,
  source_ref TEXT, extraction_id TEXT, workspace_key TEXT, session_id TEXT,
  run_id TEXT, valid INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
  invalidated_at TEXT, UNIQUE(memory_id,source_kind,source_ref,extraction_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_sources_memory ON memory_sources(memory_id,valid);
CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_id TEXT NOT NULL, revision INTEGER NOT NULL, backend TEXT NOT NULL,
  model TEXT NOT NULL, dimensions INTEGER NOT NULL, vector BLOB NOT NULL,
  content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id,revision,backend,model)
);
CREATE TABLE IF NOT EXISTS memory_recalls (
  run_id TEXT PRIMARY KEY, workspace_key TEXT NOT NULL, session_id TEXT NOT NULL,
  query_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_recall_items (
  run_id TEXT NOT NULL, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
  score REAL NOT NULL, lexical_rank INTEGER, semantic_rank INTEGER,
  reasons TEXT NOT NULL, PRIMARY KEY(run_id,memory_id)
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
            if version == 1:
                self._upgrade_v1_to_v2()
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

    def _upgrade_v1_to_v2(self) -> None:
        columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(memories)")}
        if "origin" not in columns:
            self.connection.execute("ALTER TABLE memories ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
        if "source_valid" not in columns:
            self.connection.execute("ALTER TABLE memories ADD COLUMN source_valid INTEGER NOT NULL DEFAULT 1")
        settings = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(memory_workspace_settings)")
        }
        additions = {
            "policy": "TEXT NOT NULL DEFAULT 'review'",
            "recall_enabled": "INTEGER NOT NULL DEFAULT 1",
            "embedding_backend": "TEXT NOT NULL DEFAULT 'off'",
            "embedding_model": "TEXT",
            "embedding_endpoint": "TEXT",
        }
        for name, definition in additions.items():
            if name not in settings:
                self.connection.execute(
                    f"ALTER TABLE memory_workspace_settings ADD COLUMN {name} {definition}"
                )

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
        origin: MemoryOrigin = MemoryOrigin.MANUAL,
        extraction_id: str | None = None,
        run_id: str | None = None,
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
                origin=origin,
            )
            self._source(
                self.connection,
                item,
                extraction_id=extraction_id,
                workspace=workspace,
                session_id=session_id,
                run_id=run_id,
            )
            self.connection.execute("UPDATE memories SET source_valid=1 WHERE id=?", (item.id,))
            self._history(self.connection, item, "create", empty=True)
            self._audit(self.connection, item, "add")
        return item

    def import_many(self, records: list[dict[str, Any]]) -> list[MemoryInfo]:
        output: list[MemoryInfo] = []
        with self.connection:
            for record in records:
                item = self._insert(self.connection, **record)
                self._source(
                    self.connection,
                    item,
                    extraction_id=None,
                    workspace=record.get("workspace"),
                    session_id=record.get("session_id"),
                    run_id=None,
                )
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
        origin: MemoryOrigin = MemoryOrigin.MANUAL,
    ) -> MemoryInfo:
        identifier, created = f"mem_{uuid.uuid4().hex}", timestamp()
        connection.execute(
            """INSERT INTO memories(
              id,content,memory_type,scope,workspace_key,session_id,source_kind,source_ref,
              confidence,expires_at,revision,status,created_at,updated_at,origin,source_valid
            ) VALUES(?,?,?,?,?,?,?,?,?,?,1,'active',?,?,?,1)""",
            (
                identifier, content, memory_type.value, scope.value, workspace, session_id,
                source_kind, source_ref, confidence, expires_at, created, created, origin.value,
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
            self.connection.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (current.id,))
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
            self.connection.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (current.id,))
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
                self.connection.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (current.id,))
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
            self.connection.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (current.id,))
            self.connection.execute("DELETE FROM memory_sources WHERE memory_id=?", (current.id,))
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
        return [item for item, _ in self.search_ranked(query, workspace=workspace, session_id=session_id, limit=limit)]

    def search_ranked(
        self, query: str, *, workspace: str, session_id: str, limit: int = 20
    ) -> list[tuple[MemoryInfo, int]]:
        where, values = self._visible_where(workspace, session_id, alias="m")
        active = "m.status='active' AND (m.expires_at IS NULL OR m.expires_at>?)"
        now = timestamp()
        tokens = re.findall(r"[\w.-]+", query, flags=re.UNICODE)[:12]
        phrase = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        rows: list[sqlite3.Row] = []
        try:
            if not phrase:
                raise sqlite3.OperationalError("empty full-text query")
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
        fallback_terms = [query]
        fallback_terms.extend(token for token in tokens if len(token) >= 3)
        for sequence in re.findall(r"[\u3400-\u9fff]+", query):
            fallback_terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
        fallback_terms = list(dict.fromkeys(fallback_terms))[:24]
        escaped = [
            term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            for term in fallback_terms
        ]
        like = " OR ".join("m.content LIKE ? ESCAPE '\\'" for _ in escaped)
        fallback = self.connection.execute(
            f"""SELECT m.* FROM memories m WHERE {where} AND {active}
                AND ({like}) ORDER BY m.updated_at DESC LIMIT ?""",
            (*values, now, *(f"%{term}%" for term in escaped), limit),
        ).fetchall()
        rows.extend(row for row in fallback if str(row["id"]) not in seen)
        return [(_memory_info(row), index) for index, row in enumerate(rows[:limit], start=1)]

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
                 OR (m.origin='automatic' AND m.source_valid=0)
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

    def memory_settings(self, workspace: str) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT * FROM memory_workspace_settings WHERE workspace_key=?", (workspace,)
        ).fetchone()
        return {
            "write_enabled": True if row is None else bool(row["write_enabled"]),
            "policy": MemoryPolicy.REVIEW if row is None else MemoryPolicy(row["policy"]),
            "recall_enabled": True if row is None else bool(row["recall_enabled"]),
            "embedding_backend": EmbeddingBackend.OFF
            if row is None
            else EmbeddingBackend(row["embedding_backend"]),
            "embedding_model": None if row is None else row["embedding_model"],
            "embedding_endpoint": None if row is None else row["embedding_endpoint"],
        }

    def set_policy(self, workspace: str, policy: MemoryPolicy) -> None:
        self._update_setting(workspace, "policy", policy.value)
        self._audit_setting(workspace, "capture_policy", policy.value)

    def set_recall_enabled(self, workspace: str, enabled: bool) -> None:
        self._update_setting(workspace, "recall_enabled", int(enabled))
        self._audit_setting(workspace, "recall", "enabled" if enabled else "disabled")

    def set_embedding_settings(
        self,
        workspace: str,
        backend: EmbeddingBackend,
        *,
        model: str | None,
        endpoint: str | None,
    ) -> None:
        with self.connection:
            self._ensure_settings(workspace)
            self.connection.execute(
                """UPDATE memory_workspace_settings
                   SET embedding_backend=?,embedding_model=?,embedding_endpoint=?
                   WHERE workspace_key=?""",
                (backend.value, model, endpoint, workspace),
            )
            self.connection.execute(
                "INSERT INTO memory_audit(operation,workspace_key,detail,created_at) VALUES('embeddings',?,?,?)",
                (workspace, f"backend={backend.value};model={model or '-'}", timestamp()),
            )

    def start_extraction(
        self,
        *,
        workspace: str,
        session_id: str,
        source_run_id: str,
        model: str,
        prompt_version: str,
        policy: MemoryPolicy,
    ) -> str:
        identifier = f"ext_{uuid.uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """INSERT INTO memory_extractions(
                   id,workspace_key,session_id,source_run_id,model,prompt_version,policy,status,created_at
                   ) VALUES(?,?,?,?,?,?,?,'running',?)""",
                (
                    identifier,
                    workspace,
                    session_id,
                    source_run_id,
                    model,
                    prompt_version,
                    policy.value,
                    timestamp(),
                ),
            )
        return identifier

    def finish_extraction(
        self,
        extraction_id: str,
        *,
        status: str,
        candidate_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_code: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE memory_extractions SET status=?,candidate_count=?,input_tokens=?,output_tokens=?,
                   error_code=?,completed_at=? WHERE id=?""",
                (
                    status,
                    candidate_count,
                    input_tokens,
                    output_tokens,
                    error_code,
                    timestamp(),
                    extraction_id,
                ),
            )

    def create_candidate(
        self,
        *,
        extraction_id: str,
        content: str,
        memory_type: MemoryType,
        scope: MemoryScope,
        workspace: str,
        session_id: str,
        source_run_id: str,
        confidence: float,
        status: MemoryCandidateStatus = MemoryCandidateStatus.PENDING,
        relation: str = "new",
        related_memory_id: str | None = None,
        risk_flags: tuple[str, ...] = (),
    ) -> MemoryCandidateInfo:
        identifier, created = f"cand_{uuid.uuid4().hex}", timestamp()
        with self.connection:
            self.connection.execute(
                """INSERT INTO memory_candidates(
                   id,extraction_id,content,memory_type,scope,workspace_key,session_id,source_run_id,
                   confidence,status,relation,related_memory_id,risk_flags,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    extraction_id,
                    content,
                    memory_type.value,
                    scope.value,
                    workspace,
                    session_id,
                    source_run_id,
                    confidence,
                    status.value,
                    relation,
                    related_memory_id,
                    json.dumps(risk_flags),
                    created,
                ),
            )
            self.connection.execute(
                """INSERT INTO memory_audit(operation,scope,workspace_key,session_id,detail,created_at)
                   VALUES('candidate',?,?,?,?,?)""",
                (scope.value, workspace, session_id, f"id={identifier};status={status.value}", created),
            )
        return self.get_candidate(identifier)  # type: ignore[return-value]

    def get_candidate(self, candidate_id: str) -> MemoryCandidateInfo | None:
        row = self.connection.execute(
            "SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        return None if row is None else _candidate_info(row)

    def resolve_candidate(self, prefix: str, *, workspace: str, session_id: str) -> MemoryCandidateInfo:
        rows = self.connection.execute(
            """SELECT * FROM memory_candidates WHERE workspace_key=? AND session_id=?
               AND (id=? OR id LIKE ?) ORDER BY created_at""",
            (workspace, session_id, prefix, f"{prefix}%"),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("candidate id prefix is ambiguous; provide more characters")
        if not rows:
            raise ValueError("memory candidate does not exist in this session")
        return _candidate_info(rows[0])

    def list_candidates(
        self, *, workspace: str, session_id: str, include_all: bool = False, limit: int = 200
    ) -> list[MemoryCandidateInfo]:
        query = "SELECT * FROM memory_candidates WHERE workspace_key=? AND session_id=?"
        values: list[object] = [workspace, session_id]
        if not include_all:
            query += " AND status IN ('pending','conflict')"
        query += " ORDER BY created_at LIMIT ?"
        values.append(limit)
        return [_candidate_info(row) for row in self.connection.execute(query, values).fetchall()]

    def decide_candidate(
        self,
        candidate_id: str,
        status: MemoryCandidateStatus,
        *,
        adopted_memory_id: str | None = None,
        clear_content: bool = False,
    ) -> MemoryCandidateInfo:
        with self.connection:
            self.connection.execute(
                """UPDATE memory_candidates SET status=?,adopted_memory_id=?,decided_at=?,
                   content=CASE WHEN ? THEN NULL ELSE content END WHERE id=?""",
                (status.value, adopted_memory_id, timestamp(), int(clear_content), candidate_id),
            )
            self.connection.execute(
                "INSERT INTO memory_audit(operation,memory_id,detail,created_at) VALUES('candidate_decision',?,?,?)",
                (adopted_memory_id, f"candidate={candidate_id};status={status.value}", timestamp()),
            )
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError("memory candidate does not exist")
        return candidate

    def purge_candidate(self, candidate_id: str) -> MemoryCandidateInfo:
        with self.connection:
            self.connection.execute(
                """UPDATE memory_candidates SET content=NULL,status='purged',risk_flags='[]',decided_at=?
                   WHERE id=?""",
                (timestamp(), candidate_id),
            )
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError("memory candidate does not exist")
        return candidate

    def add_source(
        self,
        memory_id: str,
        *,
        source_kind: str,
        source_ref: str | None,
        extraction_id: str | None,
        workspace: str | None,
        session_id: str | None,
        run_id: str | None,
    ) -> None:
        with self.connection:
            item = self._required(memory_id)
            self._source(
                self.connection,
                item,
                source_kind=source_kind,
                source_ref=source_ref,
                extraction_id=extraction_id,
                workspace=workspace,
                session_id=session_id,
                run_id=run_id,
            )
            self.connection.execute("UPDATE memories SET source_valid=1 WHERE id=?", (item.id,))

    def invalidate_source(self, memory_id: str, *, run_id: str | None = None) -> None:
        with self.connection:
            values: list[object] = [timestamp(), memory_id]
            suffix = ""
            if run_id is not None:
                suffix = " AND run_id=?"
                values.append(run_id)
            self.connection.execute(
                f"UPDATE memory_sources SET valid=0,invalidated_at=? WHERE memory_id=?{suffix}", values
            )
            valid = self.connection.execute(
                "SELECT 1 FROM memory_sources WHERE memory_id=? AND valid=1", (memory_id,)
            ).fetchone()
            self.connection.execute(
                "UPDATE memories SET source_valid=? WHERE id=?", (int(valid is not None), memory_id)
            )

    def sources(self, memory_id: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """SELECT source_kind,source_ref,extraction_id,workspace_key,session_id,run_id,valid,created_at
               FROM memory_sources WHERE memory_id=? ORDER BY id""",
            (memory_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def put_embedding(
        self,
        item: MemoryInfo,
        *,
        backend: EmbeddingBackend,
        model: str,
        dimensions: int,
        vector: bytes,
        content_hash: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO memory_embeddings(
                   memory_id,revision,backend,model,dimensions,vector,content_hash,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    item.id,
                    item.revision,
                    backend.value,
                    model,
                    dimensions,
                    vector,
                    content_hash,
                    timestamp(),
                ),
            )

    def embeddings(
        self, *, workspace: str, session_id: str, backend: EmbeddingBackend, model: str
    ) -> list[tuple[MemoryInfo, bytes, int]]:
        where, values = self._visible_where(workspace, session_id, alias="m")
        rows = self.connection.execute(
            f"""SELECT m.*,e.vector,e.dimensions FROM memory_embeddings e
                JOIN memories m ON m.id=e.memory_id AND m.revision=e.revision
                WHERE e.backend=? AND e.model=? AND {where} AND m.status='active'
                  AND (m.expires_at IS NULL OR m.expires_at>?)""",
            (backend.value, model, *values, timestamp()),
        ).fetchall()
        return [(_memory_info(row), bytes(row["vector"]), int(row["dimensions"])) for row in rows]

    def clear_embeddings(self, *, workspace: str | None = None) -> int:
        with self.connection:
            if workspace is None:
                return self.connection.execute("DELETE FROM memory_embeddings").rowcount
            visible = self.connection.execute(
                "SELECT id FROM memories WHERE scope='global' OR workspace_key=?", (workspace,)
            ).fetchall()
            identifiers = [str(row[0]) for row in visible]
            if not identifiers:
                return 0
            return self.connection.execute(
                "DELETE FROM memory_embeddings WHERE memory_id IN ("
                + ",".join("?" for _ in identifiers)
                + ")",
                identifiers,
            ).rowcount

    def record_recall(
        self,
        *,
        workspace: str,
        session_id: str,
        run_id: str,
        query: str,
        hits: list[MemoryRecallHit],
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO memory_recalls VALUES(?,?,?,?,?)",
                (run_id, workspace, session_id, hashlib.sha256(query.encode()).hexdigest(), timestamp()),
            )
            self.connection.execute("DELETE FROM memory_recall_items WHERE run_id=?", (run_id,))
            self.connection.executemany(
                """INSERT INTO memory_recall_items(
                   run_id,memory_id,revision,score,lexical_rank,semantic_rank,reasons
                   ) VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        run_id,
                        hit.memory.id,
                        hit.memory.revision,
                        hit.score,
                        hit.lexical_rank,
                        hit.semantic_rank,
                        json.dumps(hit.reasons, ensure_ascii=False),
                    )
                    for hit in hits
                ],
            )
        self.record_access(
            [hit.memory for hit in hits], workspace=workspace, session_id=session_id, run_id=run_id
        )

    def recall_hits(
        self, *, workspace: str, session_id: str, run_id: str | None = None
    ) -> list[MemoryRecallHit]:
        if run_id is None:
            row = self.connection.execute(
                """SELECT run_id FROM memory_recalls WHERE workspace_key=? AND session_id=?
                   ORDER BY created_at DESC LIMIT 1""",
                (workspace, session_id),
            ).fetchone()
            if row is None:
                return []
            run_id = str(row[0])
        rows = self.connection.execute(
            """SELECT i.*,m.* FROM memory_recall_items i JOIN memories m ON m.id=i.memory_id
               JOIN memory_recalls r ON r.run_id=i.run_id
               WHERE i.run_id=? AND r.workspace_key=? AND r.session_id=? ORDER BY i.score DESC""",
            (run_id, workspace, session_id),
        ).fetchall()
        return [
            MemoryRecallHit(
                _memory_info(row),
                float(row["score"]),
                row["lexical_rank"],
                row["semantic_rank"],
                tuple(json.loads(row["reasons"])),
            )
            for row in rows
        ]

    def cleanup(self, *, workspace: str, retention_days: int = 30) -> dict[str, int]:
        cutoff = datetime.now(UTC).timestamp() - retention_days * 86400
        cutoff_text = datetime.fromtimestamp(cutoff, UTC).isoformat()
        with self.connection:
            expired = self.connection.execute(
                """DELETE FROM memory_embeddings WHERE memory_id IN (
                   SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at<=?
                   )""",
                (timestamp(),),
            ).rowcount
            candidates = self.connection.execute(
                """UPDATE memory_candidates SET content=NULL,risk_flags='[]'
                   WHERE workspace_key=? AND status IN ('rejected','duplicate')
                     AND decided_at IS NOT NULL AND decided_at<=? AND content IS NOT NULL""",
                (workspace, cutoff_text),
            ).rowcount
        return {"expired_embeddings": expired, "candidate_contents": candidates}

    def _ensure_settings(self, workspace: str) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO memory_workspace_settings(
               workspace_key,write_enabled,policy,recall_enabled,embedding_backend
               ) VALUES(?,1,'review',1,'off')""",
            (workspace,),
        )

    def _update_setting(self, workspace: str, name: str, value: object) -> None:
        if name not in {"policy", "recall_enabled"}:
            raise ValueError("unsupported memory setting")
        with self.connection:
            self._ensure_settings(workspace)
            self.connection.execute(
                f"UPDATE memory_workspace_settings SET {name}=? WHERE workspace_key=?", (value, workspace)
            )

    def _audit_setting(self, workspace: str, name: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO memory_audit(operation,workspace_key,detail,created_at) VALUES('policy',?,?,?)",
                (workspace, f"{name}={value}", timestamp()),
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

    def _source(
        self,
        connection: sqlite3.Connection,
        item: MemoryInfo,
        *,
        source_kind: str | None = None,
        source_ref: str | None = None,
        extraction_id: str | None,
        workspace: str | None,
        session_id: str | None,
        run_id: str | None,
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO memory_sources(
               memory_id,source_kind,source_ref,extraction_id,workspace_key,session_id,run_id,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                item.id,
                source_kind or item.source_kind,
                item.source_ref if source_ref is None else source_ref,
                extraction_id,
                workspace,
                session_id,
                run_id,
                timestamp(),
            ),
        )

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
    keys = set(row.keys())
    return MemoryInfo(
        id=str(row["id"]), content=row["content"], type=MemoryType(row["memory_type"]),
        scope=MemoryScope(row["scope"]), workspace_key=row["workspace_key"], session_id=row["session_id"],
        source_kind=str(row["source_kind"]), source_ref=row["source_ref"], confidence=float(row["confidence"]),
        expires_at=row["expires_at"], revision=int(row["revision"]), status=MemoryStatus(row["status"]),
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]), purged_at=row["purged_at"],
        origin=MemoryOrigin(row["origin"]) if "origin" in keys else MemoryOrigin.MANUAL,
        source_valid=bool(row["source_valid"]) if "source_valid" in keys else True,
    )


def _candidate_info(row: sqlite3.Row) -> MemoryCandidateInfo:
    return MemoryCandidateInfo(
        id=str(row["id"]),
        extraction_id=str(row["extraction_id"]),
        content=row["content"],
        type=MemoryType(row["memory_type"]),
        scope=MemoryScope(row["scope"]),
        workspace_key=str(row["workspace_key"]),
        session_id=str(row["session_id"]),
        source_run_id=str(row["source_run_id"]),
        confidence=float(row["confidence"]),
        status=MemoryCandidateStatus(row["status"]),
        relation=str(row["relation"]),
        related_memory_id=row["related_memory_id"],
        risk_flags=tuple(json.loads(row["risk_flags"] or "[]")),
        adopted_memory_id=row["adopted_memory_id"],
        created_at=str(row["created_at"]),
        decided_at=row["decided_at"],
    )
