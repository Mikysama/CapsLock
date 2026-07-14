"""SQLite-backed, single-user session and trace persistence."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SessionInfo:
    id: str
    workspace: Path
    model: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChangeInfo:
    id: str
    session_id: str
    run_id: str
    path: str
    operation: str
    expected_hash: str | None
    before_content: str | None
    after_content: str
    diff: str
    summary: str
    status: str
    created_at: str


class SessionStore:
    def __init__(self, database: str | Path) -> None:
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, workspace TEXT NOT NULL, model TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, summary TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
              content TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, question TEXT NOT NULL,
              status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
              duration_ms INTEGER, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
              error TEXT
            );
            CREATE TABLE IF NOT EXISTS tool_calls (
              id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL,
              arguments TEXT NOT NULL, ok INTEGER NOT NULL, result_summary TEXT NOT NULL,
              duration_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS citations (
              id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, citation_id TEXT NOT NULL,
              path TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS changes (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
              path TEXT NOT NULL, operation TEXT NOT NULL, expected_hash TEXT,
              before_content TEXT, after_content TEXT NOT NULL, diff TEXT NOT NULL,
              summary TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
              approved_at TEXT, applied_at TEXT, undone_at TEXT, error TEXT
            );
            """
        )
        self._connection.commit()

    def create(self, workspace: Path, model: str) -> SessionInfo:
        session_id, now = uuid.uuid4().hex, _now()
        self._connection.execute(
            "INSERT INTO sessions(id, workspace, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, str(workspace.resolve()), model, now, now),
        )
        self._connection.commit()
        return SessionInfo(session_id, workspace.resolve(), model, now, now)

    def get(self, session_id: str) -> SessionInfo | None:
        row = self._connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return None if row is None else SessionInfo(row["id"], Path(row["workspace"]), row["model"], row["created_at"], row["updated_at"])

    def list(self, limit: int = 20) -> list[SessionInfo]:
        rows = self._connection.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [SessionInfo(row["id"], Path(row["workspace"]), row["model"], row["created_at"], row["updated_at"]) for row in rows]

    def messages(self, session_id: str, limit: int = 24) -> list[dict[str, str]]:
        rows = self._connection.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?", (session_id, limit)
        ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def append_message(self, session_id: str, role: str, content: str) -> None:
        self._connection.execute("INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)", (session_id, role, content, _now()))
        self._connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))
        self._connection.commit()

    def start_run(self, session_id: str, question: str) -> str:
        run_id = uuid.uuid4().hex
        self._connection.execute("INSERT INTO runs(id, session_id, question, status, started_at) VALUES (?, ?, ?, 'running', ?)", (run_id, session_id, question, _now()))
        self._connection.commit()
        return run_id

    def finish_run(self, run_id: str, *, status: str, duration_ms: int, error: str | None = None) -> None:
        self._connection.execute("UPDATE runs SET status=?, finished_at=?, duration_ms=?, error=? WHERE id=?", (status, _now(), duration_ms, error, run_id))
        self._connection.commit()

    def record_tool_call(self, run_id: str, name: str, arguments: dict[str, Any], ok: bool, summary: str, duration_ms: int) -> None:
        self._connection.execute(
            "INSERT INTO tool_calls(run_id,name,arguments,ok,result_summary,duration_ms) VALUES(?,?,?,?,?,?)",
            (run_id, name, json.dumps(arguments, ensure_ascii=False), ok, summary[:1_000], duration_ms),
        )
        self._connection.commit()

    def record_citations(self, run_id: str, citations: list[Any]) -> None:
        self._connection.executemany(
            "INSERT INTO citations(run_id,citation_id,path,start_line,end_line) VALUES(?,?,?,?,?)",
            [(run_id, item.id, str(item.path), item.start_line, item.end_line) for item in citations],
        )
        self._connection.commit()

    def create_change(
        self, *, session_id: str, run_id: str, path: str, operation: str,
        expected_hash: str | None, before_content: str | None, after_content: str,
        diff: str, summary: str,
    ) -> ChangeInfo:
        change_id, now = uuid.uuid4().hex, _now()
        self._connection.execute(
            """INSERT INTO changes(id,session_id,run_id,path,operation,expected_hash,before_content,
               after_content,diff,summary,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
            (change_id, session_id, run_id, path, operation, expected_hash, before_content,
             after_content, diff, summary, now),
        )
        self._connection.commit()
        return self.get_change(change_id, session_id=session_id)  # type: ignore[return-value]

    def get_change(self, change_id: str, *, session_id: str | None = None) -> ChangeInfo | None:
        query, values = "SELECT * FROM changes WHERE id = ?", [change_id]
        if session_id is not None:
            query += " AND session_id = ?"
            values.append(session_id)
        row = self._connection.execute(query, values).fetchone()
        return None if row is None else _change_info(row)

    def list_changes(self, session_id: str, *, statuses: tuple[str, ...] | None = None) -> list[ChangeInfo]:
        query, values = "SELECT * FROM changes WHERE session_id = ?", [session_id]
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            values.extend(statuses)
        query += " ORDER BY created_at"
        return [_change_info(row) for row in self._connection.execute(query, values).fetchall()]

    def update_change_status(self, change_id: str, status: str, *, error: str | None = None) -> None:
        timestamp_column = {"approved": "approved_at", "applied": "applied_at", "undone": "undone_at"}.get(status)
        if timestamp_column:
            self._connection.execute(f"UPDATE changes SET status=?, {timestamp_column}=?, error=? WHERE id=?", (status, _now(), error, change_id))
        else:
            self._connection.execute("UPDATE changes SET status=?, error=? WHERE id=?", (status, error, change_id))
        self._connection.commit()

    def last_applied_change(self, session_id: str) -> ChangeInfo | None:
        row = self._connection.execute(
            "SELECT * FROM changes WHERE session_id=? AND status='applied' ORDER BY applied_at DESC LIMIT 1", (session_id,)
        ).fetchone()
        return None if row is None else _change_info(row)


def _change_info(row: sqlite3.Row) -> ChangeInfo:
    return ChangeInfo(
        row["id"], row["session_id"], row["run_id"], row["path"], row["operation"], row["expected_hash"],
        row["before_content"], row["after_content"], row["diff"], row["summary"], row["status"], row["created_at"],
    )
