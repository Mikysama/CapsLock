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


@dataclass(frozen=True)
class CommandInfo:
    id: str
    session_id: str
    run_id: str
    template: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: float
    summary: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class TaskInfo:
    id: str
    session_id: str
    text: str
    status: str


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
              error TEXT, cost_usd REAL DEFAULT 0
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
            CREATE TABLE IF NOT EXISTS commands (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
              template TEXT NOT NULL, argv TEXT NOT NULL, cwd TEXT NOT NULL,
              timeout_seconds REAL NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
              created_at TEXT NOT NULL, approved_at TEXT, started_at TEXT, finished_at TEXT,
              exit_code INTEGER, stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '',
              output_truncated INTEGER NOT NULL DEFAULT 0, error TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, text TEXT NOT NULL,
              status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        self._ensure_column("runs", "cost_usd", "REAL DEFAULT 0")
        self._connection.commit()

    def _ensure_column(self, table: str, name: str, definition: str) -> None:
        columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

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

    def finish_run(self, run_id: str, *, status: str, duration_ms: int, error: str | None = None, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0) -> None:
        self._connection.execute("UPDATE runs SET status=?, finished_at=?, duration_ms=?, error=?, input_tokens=?, output_tokens=?, cost_usd=? WHERE id=?", (status, _now(), duration_ms, error, input_tokens, output_tokens, cost_usd, run_id))
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

    def create_command(self, *, session_id: str, run_id: str, template: str, argv: list[str], cwd: str, timeout_seconds: float, summary: str) -> CommandInfo:
        command_id, now = uuid.uuid4().hex, _now()
        self._connection.execute(
            "INSERT INTO commands(id,session_id,run_id,template,argv,cwd,timeout_seconds,summary,status,created_at) VALUES(?,?,?,?,?,?,?,?, 'pending', ?)",
            (command_id, session_id, run_id, template, json.dumps(argv), cwd, timeout_seconds, summary, now),
        )
        self._connection.commit()
        return self.get_command(command_id, session_id=session_id)  # type: ignore[return-value]

    def get_command(self, command_id: str, *, session_id: str | None = None) -> CommandInfo | None:
        query, values = "SELECT * FROM commands WHERE id=?", [command_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = self._connection.execute(query, values).fetchone()
        return None if row is None else _command_info(row)

    def list_commands(self, session_id: str) -> list[CommandInfo]:
        return [_command_info(row) for row in self._connection.execute("SELECT * FROM commands WHERE session_id=? ORDER BY created_at", (session_id,)).fetchall()]

    def update_command(self, command_id: str, status: str, *, exit_code: int | None = None, stdout: str = "", stderr: str = "", truncated: bool = False, error: str | None = None) -> None:
        timestamp = {"approved": "approved_at", "running": "started_at", "completed": "finished_at", "failed": "finished_at", "cancelled": "finished_at"}.get(status)
        if timestamp:
            self._connection.execute(f"UPDATE commands SET status=?, {timestamp}=?, exit_code=?, stdout=?, stderr=?, output_truncated=?, error=? WHERE id=?", (status, _now(), exit_code, stdout, stderr, int(truncated), error, command_id))
        else:
            self._connection.execute("UPDATE commands SET status=?, error=? WHERE id=?", (status, error, command_id))
        self._connection.commit()

    def replace_tasks(self, session_id: str, items: list[str]) -> list[TaskInfo]:
        now = _now()
        existing = {row["text"]: row for row in self._connection.execute("SELECT * FROM tasks WHERE session_id=? AND status IN ('pending','running','blocked')", (session_id,))}
        for text in items:
            if text not in existing:
                self._connection.execute("INSERT INTO tasks(id,session_id,text,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, session_id, text, "pending", now, now))
        for text, row in existing.items():
            if text not in items:
                self._connection.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?", (now, row["id"]))
        self._connection.commit()
        return self.list_tasks(session_id)

    def list_tasks(self, session_id: str) -> list[TaskInfo]:
        return [TaskInfo(row["id"], row["session_id"], row["text"], row["status"]) for row in self._connection.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY created_at", (session_id,))]

    def update_task_status(self, task_id: str, session_id: str, status: str) -> TaskInfo:
        if status not in {"pending", "running", "blocked", "completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported task status: {status}")
        updated = self._connection.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=? AND session_id=?", (status, _now(), task_id, session_id)
        ).rowcount
        if not updated:
            raise ValueError("task does not belong to this session or does not exist")
        self._connection.commit()
        row = self._connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return TaskInfo(row["id"], row["session_id"], row["text"], row["status"])

    def message_count(self, session_id: str) -> int:
        return int(self._connection.execute("SELECT count(*) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0])

    def compact_summary(self, session_id: str, keep: int) -> str:
        rows = self._connection.execute("SELECT role,content FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        older = rows[:-keep] if len(rows) > keep else []
        if not older:
            return self._connection.execute("SELECT summary FROM sessions WHERE id=?", (session_id,)).fetchone()[0]
        summary = "\n".join(f"{row['role']}: {row['content']}" for row in older)
        summary = summary[-6000:]
        self._connection.execute("UPDATE sessions SET summary=?, updated_at=? WHERE id=?", (summary, _now(), session_id))
        self._connection.commit()
        return summary

    def session_cost(self, session_id: str) -> tuple[int, int, float]:
        row = self._connection.execute("SELECT coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), coalesce(sum(cost_usd),0) FROM runs WHERE session_id=?", (session_id,)).fetchone()
        return int(row[0]), int(row[1]), float(row[2])


def _change_info(row: sqlite3.Row) -> ChangeInfo:
    return ChangeInfo(
        row["id"], row["session_id"], row["run_id"], row["path"], row["operation"], row["expected_hash"],
        row["before_content"], row["after_content"], row["diff"], row["summary"], row["status"], row["created_at"],
    )


def _command_info(row: sqlite3.Row) -> CommandInfo:
    return CommandInfo(row["id"], row["session_id"], row["run_id"], row["template"], tuple(json.loads(row["argv"])), row["cwd"], row["timeout_seconds"], row["summary"], row["status"], row["exit_code"], row["stdout"], row["stderr"])
