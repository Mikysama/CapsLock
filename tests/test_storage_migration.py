import json
import sqlite3
from pathlib import Path

import pytest

from capslock.domain import ActionResultKind, ActionStatus
from capslock.session import SessionStore


LEGACY_ACTION_SCHEMA = """
CREATE TABLE changes (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
  path TEXT NOT NULL, operation TEXT NOT NULL, expected_hash TEXT,
  before_content TEXT, after_content TEXT NOT NULL, diff TEXT NOT NULL,
  summary TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
  approved_at TEXT, applied_at TEXT, undone_at TEXT, error TEXT
);
CREATE TABLE commands (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
  template TEXT NOT NULL, argv TEXT NOT NULL, cwd TEXT NOT NULL,
  timeout_seconds REAL NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, approved_at TEXT, started_at TEXT, finished_at TEXT,
  exit_code INTEGER, stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '',
  output_truncated INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE external_actions (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
  kind TEXT NOT NULL, payload TEXT NOT NULL, summary TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, approved_at TEXT,
  started_at TEXT, finished_at TEXT, result TEXT, error TEXT
);
"""


def legacy_database(path: Path, *, duplicate_id: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_ACTION_SCHEMA)
    change_id = "same" if duplicate_id else "change"
    command_id = "same" if duplicate_id else "command"
    connection.execute(
        "INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (change_id, "session", "run", "note.txt", "edit", "hash", "before", "after", "diff", "edit", "applied", "2026-01-01", "2026-01-02", "2026-01-03", None, None),
    )
    connection.execute(
        "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (command_id, "session", "run", "pytest", json.dumps(["pytest"]), ".", 10, "test", "failed", "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", 1, "", "failed", 0, "exit"),
    )
    connection.execute(
        "INSERT INTO external_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("external", "session", "run", "web_search", json.dumps({"query": "capslock"}), "search", "completed", "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", json.dumps({"results": []}), None),
    )
    connection.commit()
    connection.close()


def test_legacy_actions_migrate_with_backup_and_normalized_statuses(tmp_path: Path) -> None:
    path = tmp_path / "capslock.sqlite3"
    legacy_database(path)

    with SessionStore(path) as store:
        change = store.get_change("change")
        command = store.get_command("command")
        external = store.get_external_action("external")
        assert change.status is ActionStatus.COMPLETED
        assert change.result_kind is ActionResultKind.APPLIED
        assert command.status is ActionStatus.FAILED
        assert command.result_kind is ActionResultKind.NONZERO_EXIT
        assert external.status is ActionStatus.COMPLETED
        assert external.result_kind is ActionResultKind.SUCCESS
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "run_id" in {row[1] for row in store._connection.execute("PRAGMA table_info(messages)")}
        assert store._connection.execute("SELECT count(*) FROM actions").fetchone()[0] == 3

    backups = list((tmp_path / "backups").glob("capslock-schema-v0-*.sqlite3"))
    assert len(backups) == 1
    with SessionStore(path):
        pass
    assert list((tmp_path / "backups").glob("capslock-schema-v0-*.sqlite3")) == backups


def test_duplicate_legacy_action_ids_roll_back_and_keep_backup(tmp_path: Path) -> None:
    path = tmp_path / "capslock.sqlite3"
    legacy_database(path, duplicate_id=True)

    with pytest.raises(RuntimeError, match="backup retained"):
        SessionStore(path)

    connection = sqlite3.connect(path)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    assert {"changes", "commands", "external_actions"} <= tables
    assert "actions" not in tables
    assert len(list((tmp_path / "backups").glob("capslock-schema-v0-*.sqlite3"))) == 1


def test_schema_v1_adds_run_ids_with_one_backup(tmp_path: Path) -> None:
    path = tmp_path / "capslock.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
          content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        PRAGMA user_version = 1;
    """)
    connection.execute(
        "INSERT INTO messages(session_id,role,content,created_at) VALUES('s','user','kept','now')"
    )
    connection.commit()
    connection.close()

    with SessionStore(path) as store:
        columns = {row[1] for row in store._connection.execute("PRAGMA table_info(messages)")}
        assert "run_id" in columns
        assert store.messages("s") == [{"role": "user", "content": "kept"}]
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 2

    backups = list((tmp_path / "backups").glob("capslock-schema-v1-*.sqlite3"))
    assert len(backups) == 1
    with SessionStore(path):
        pass
    assert list((tmp_path / "backups").glob("capslock-schema-v1-*.sqlite3")) == backups
