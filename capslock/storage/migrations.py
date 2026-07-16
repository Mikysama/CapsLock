"""Versioned SQLite schema creation and legacy action migration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..domain import SessionTitleSource, normalize_session_title, pending_session_title


SCHEMA_VERSION = 5

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, model TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '', title_source TEXT NOT NULL DEFAULT 'pending',
  title_updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, created_at TEXT NOT NULL, run_id TEXT
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
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, text TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
  url TEXT NOT NULL, title TEXT NOT NULL, excerpt TEXT NOT NULL,
  fetched_at TEXT NOT NULL, suspicious INTEGER NOT NULL DEFAULT 0
);
"""

ACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
  action_type TEXT NOT NULL, status TEXT NOT NULL, result_kind TEXT,
  summary TEXT NOT NULL, created_at TEXT NOT NULL, approved_at TEXT,
  started_at TEXT, finished_at TEXT, reversed_at TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_session_created ON actions(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id);
CREATE TABLE IF NOT EXISTS file_action_data (
  action_id TEXT PRIMARY KEY, path TEXT NOT NULL, operation TEXT NOT NULL,
  expected_hash TEXT, before_content TEXT, after_content TEXT NOT NULL,
  diff TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command_action_data (
  action_id TEXT PRIMARY KEY, template TEXT NOT NULL, argv TEXT NOT NULL,
  cwd TEXT NOT NULL, timeout_seconds REAL NOT NULL, exit_code INTEGER,
  stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '',
  output_truncated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS external_action_data (
  action_id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT
);
"""

SKILL_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_settings (
  name TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def migrate(connection: sqlite3.Connection, path: Path) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {version} is newer than supported schema {SCHEMA_VERSION}")
    if version == SCHEMA_VERSION:
        connection.executescript(BASE_SCHEMA + ACTION_SCHEMA + SKILL_SETTINGS_SCHEMA)
        _remove_legacy_skill_schema(connection)
        _migrate_messages_v2(connection)
        _migrate_sessions_v3(connection)
        connection.commit()
        return
    tables = _tables(connection)
    if not tables:
        connection.executescript(BASE_SCHEMA + ACTION_SCHEMA + SKILL_SETTINGS_SCHEMA)
        _migrate_messages_v2(connection)
        _migrate_sessions_v3(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
        return

    backup = _backup(connection, path, version)
    try:
        connection.execute("BEGIN IMMEDIATE")
        legacy = {"changes", "commands", "external_actions"} & tables
        if legacy:
            _migrate_legacy_actions(connection, legacy)
        _execute_script(connection, BASE_SCHEMA + ACTION_SCHEMA + SKILL_SETTINGS_SCHEMA)
        _remove_legacy_skill_schema(connection)
        _migrate_messages_v2(connection)
        _migrate_sessions_v3(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        raise RuntimeError(f"database migration failed; backup retained at {backup}") from exc


def _remove_legacy_skill_schema(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS skill_runs")


def _migrate_messages_v2(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")}
    if "run_id" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN run_id TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_run ON messages(session_id, run_id)")


def _migrate_sessions_v3(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")}
    if "title" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    if "title_source" not in columns:
        connection.execute(
            "ALTER TABLE sessions ADD COLUMN title_source TEXT NOT NULL DEFAULT 'pending'"
        )
    if "title_updated_at" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN title_updated_at TEXT")

    sessions = connection.execute(
        "SELECT id,created_at,title FROM sessions WHERE trim(title)=''"
    ).fetchall()
    for session in sessions:
        first_run = connection.execute(
            "SELECT question,started_at FROM runs WHERE session_id=? ORDER BY started_at,rowid LIMIT 1",
            (session["id"],),
        ).fetchone()
        try:
            title = normalize_session_title(first_run["question"], truncate=True) if first_run else None
        except ValueError:
            title = None
        if title:
            source = SessionTitleSource.FIRST_QUESTION.value
            updated_at = first_run["started_at"]
        else:
            title = pending_session_title(session["created_at"])
            source = SessionTitleSource.PENDING.value
            updated_at = session["created_at"]
        connection.execute(
            "UPDATE sessions SET title=?,title_source=?,title_updated_at=? WHERE id=?",
            (title, source, updated_at, session["id"]),
        )


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _backup(connection: sqlite3.Connection, path: Path, version: int) -> Path:
    directory = path.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = directory / f"{path.stem}-schema-v{version}-{stamp}{path.suffix}"
    suffix = 1
    while backup_path.exists():
        backup_path = directory / f"{path.stem}-schema-v{version}-{stamp}-{suffix}{path.suffix}"
        suffix += 1
    target = sqlite3.connect(backup_path)
    try:
        connection.backup(target)
    finally:
        target.close()
    return backup_path


def _migrate_legacy_actions(connection: sqlite3.Connection, legacy: set[str]) -> None:
    ids: list[str] = []
    for table in sorted(legacy):
        ids.extend(str(row[0]) for row in connection.execute(f"SELECT id FROM {table}"))
    if len(ids) != len(set(ids)):
        raise ValueError("legacy action ids collide across action tables")

    for table in legacy:
        connection.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")
    _execute_script(connection, ACTION_SCHEMA)

    if "changes" in legacy:
        connection.execute("""
            INSERT INTO actions(
              id,session_id,run_id,action_type,status,result_kind,summary,created_at,
              approved_at,started_at,finished_at,reversed_at,error
            )
            SELECT id,session_id,run_id,
              CASE operation WHEN 'create' THEN 'file_create' ELSE 'file_edit' END,
              CASE status WHEN 'applied' THEN 'completed' WHEN 'undone' THEN 'completed'
                WHEN 'discarded' THEN 'rejected' ELSE status END,
              CASE status WHEN 'applied' THEN 'applied' WHEN 'undone' THEN 'undone'
                WHEN 'failed' THEN 'execution_error' END,
              summary,created_at,approved_at,applied_at,
              CASE WHEN status IN ('applied','undone') THEN coalesce(undone_at,applied_at) END,
              undone_at,error
            FROM legacy_changes
        """)
        connection.execute("""
            INSERT INTO file_action_data(action_id,path,operation,expected_hash,before_content,after_content,diff)
            SELECT id,path,operation,expected_hash,before_content,after_content,diff FROM legacy_changes
        """)
    if "commands" in legacy:
        connection.execute("""
            INSERT INTO actions(
              id,session_id,run_id,action_type,status,result_kind,summary,created_at,
              approved_at,started_at,finished_at,reversed_at,error
            )
            SELECT id,session_id,run_id,'command',status,
              CASE
                WHEN status='completed' THEN 'exit_zero'
                WHEN status='cancelled' THEN 'user_cancelled'
                WHEN status='failed' AND error LIKE '%timed out%' THEN 'timeout'
                WHEN status='failed' AND exit_code IS NOT NULL THEN 'nonzero_exit'
                WHEN status='failed' THEN 'execution_error'
              END,
              summary,created_at,approved_at,started_at,finished_at,NULL,error
            FROM legacy_commands
        """)
        connection.execute("""
            INSERT INTO command_action_data(
              action_id,template,argv,cwd,timeout_seconds,exit_code,stdout,stderr,output_truncated
            )
            SELECT id,template,argv,cwd,timeout_seconds,exit_code,stdout,stderr,output_truncated
            FROM legacy_commands
        """)
    if "external_actions" in legacy:
        connection.execute("""
            INSERT INTO actions(
              id,session_id,run_id,action_type,status,result_kind,summary,created_at,
              approved_at,started_at,finished_at,reversed_at,error
            )
            SELECT id,session_id,run_id,kind,status,
              CASE WHEN status='completed' THEN 'success'
                WHEN status='failed' THEN 'execution_error' END,
              summary,created_at,approved_at,started_at,finished_at,NULL,error
            FROM legacy_external_actions
        """)
        connection.execute("""
            INSERT INTO external_action_data(action_id,payload,result)
            SELECT id,payload,result FROM legacy_external_actions
        """)

    expected = sum(int(connection.execute(f"SELECT count(*) FROM legacy_{table}").fetchone()[0]) for table in legacy)
    actual = int(connection.execute("SELECT count(*) FROM actions").fetchone()[0])
    if actual != expected:
        raise ValueError(f"action migration count mismatch: expected {expected}, got {actual}")
    for table in legacy:
        connection.execute(f"DROP TABLE legacy_{table}")


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)
