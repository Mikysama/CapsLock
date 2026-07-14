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
