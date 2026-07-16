"""Repositories for session state, actions, sources, and settings."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain import (
    ActionInfo,
    ActionResultKind,
    ActionStatus,
    ActionType,
    ChangeInfo,
    CommandInfo,
    ExternalActionInfo,
    SessionInfo,
    SourceInfo,
    TaskInfo,
)


def now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


class SessionRepository(Repository):
    def create(self, workspace: Path, model: str) -> SessionInfo:
        session_id, created = uuid.uuid4().hex, now()
        self.connection.execute(
            "INSERT INTO sessions(id,workspace,model,created_at,updated_at) VALUES(?,?,?,?,?)",
            (session_id, str(workspace.resolve()), model, created, created),
        )
        self.connection.commit()
        return SessionInfo(session_id, workspace.resolve(), model, created, created)

    def get(self, session_id: str) -> SessionInfo | None:
        row = self.connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return None if row is None else _session_info(row)

    def list(self, limit: int = 20) -> list[SessionInfo]:
        rows = self.connection.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_session_info(row) for row in rows]

    def messages(self, session_id: str, limit: int = 24, *, excluded_run_ids: set[str] | None = None) -> list[dict[str, str]]:
        query = "SELECT role,content FROM messages WHERE session_id=?"
        values: list[Any] = [session_id]
        if excluded_run_ids:
            query += " AND (run_id IS NULL OR run_id NOT IN (" + ",".join("?" for _ in excluded_run_ids) + "))"
            values.extend(sorted(excluded_run_ids))
        query += " ORDER BY id DESC LIMIT ?"
        values.append(limit)
        rows = self.connection.execute(query, values).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def append_message(self, session_id: str, role: str, content: str, *, run_id: str | None = None) -> None:
        timestamp = now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO messages(session_id,role,content,created_at,run_id) VALUES(?,?,?,?,?)",
                (session_id, role, content, timestamp, run_id),
            )
            self.connection.execute("UPDATE sessions SET updated_at=? WHERE id=?", (timestamp, session_id))

    def message_count(self, session_id: str, *, excluded_run_ids: set[str] | None = None) -> int:
        query = "SELECT count(*) FROM messages WHERE session_id=?"
        values: list[Any] = [session_id]
        if excluded_run_ids:
            query += " AND (run_id IS NULL OR run_id NOT IN (" + ",".join("?" for _ in excluded_run_ids) + "))"
            values.extend(sorted(excluded_run_ids))
        return int(self.connection.execute(query, values).fetchone()[0])

    def compact_summary(self, session_id: str, keep: int, *, excluded_run_ids: set[str] | None = None) -> str:
        query = "SELECT role,content FROM messages WHERE session_id=?"
        values: list[Any] = [session_id]
        if excluded_run_ids:
            query += " AND (run_id IS NULL OR run_id NOT IN (" + ",".join("?" for _ in excluded_run_ids) + "))"
            values.extend(sorted(excluded_run_ids))
        query += " ORDER BY id"
        rows = self.connection.execute(query, values).fetchall()
        older = rows[:-keep] if len(rows) > keep else []
        if not older:
            return ""
        summary = "\n".join(f"{row['role']}: {row['content']}" for row in older)[-6000:]
        self.connection.execute("UPDATE sessions SET summary=?,updated_at=? WHERE id=?", (summary, now(), session_id))
        self.connection.commit()
        return summary


class RunRepository(Repository):
    def start_run(self, session_id: str, question: str) -> str:
        run_id = uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO runs(id,session_id,question,status,started_at) VALUES(?,?,?,'running',?)",
            (run_id, session_id, question, now()),
        )
        self.connection.commit()
        return run_id

    def finish_run(self, run_id: str, *, status: str, duration_ms: int, error: str | None = None, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0) -> None:
        self.connection.execute(
            "UPDATE runs SET status=?,finished_at=?,duration_ms=?,error=?,input_tokens=?,output_tokens=?,cost_usd=? WHERE id=?",
            (status, now(), duration_ms, error, input_tokens, output_tokens, cost_usd, run_id),
        )
        self.connection.commit()

    def record_tool_call(self, run_id: str, name: str, arguments: dict[str, Any], ok: bool, summary: str, duration_ms: int) -> None:
        self.connection.execute(
            "INSERT INTO tool_calls(run_id,name,arguments,ok,result_summary,duration_ms) VALUES(?,?,?,?,?,?)",
            (run_id, name, json.dumps(arguments, ensure_ascii=False), ok, summary[:1000], duration_ms),
        )
        self.connection.commit()

    def record_citations(self, run_id: str, citations: list[Any]) -> None:
        self.connection.executemany(
            "INSERT INTO citations(run_id,citation_id,path,start_line,end_line) VALUES(?,?,?,?,?)",
            [(run_id, item.id, str(item.path), item.start_line, item.end_line) for item in citations],
        )
        self.connection.commit()

    def session_cost(self, session_id: str) -> tuple[int, int, float]:
        row = self.connection.execute(
            "SELECT coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),coalesce(sum(cost_usd),0) FROM runs WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return int(row[0]), int(row[1]), float(row[2])


class ActionRepository(Repository):
    def create_change(self, *, session_id: str, run_id: str, path: str, operation: str, expected_hash: str | None, before_content: str | None, after_content: str, diff: str, summary: str) -> ChangeInfo:
        action_id, created = uuid.uuid4().hex, now()
        action_type = ActionType.FILE_CREATE if operation == "create" else ActionType.FILE_EDIT
        with self.connection:
            self.connection.execute(
                "INSERT INTO actions(id,session_id,run_id,action_type,status,summary,created_at) VALUES(?,?,?,?,?,?,?)",
                (action_id, session_id, run_id, action_type.value, ActionStatus.PENDING.value, summary, created),
            )
            self.connection.execute(
                "INSERT INTO file_action_data(action_id,path,operation,expected_hash,before_content,after_content,diff) VALUES(?,?,?,?,?,?,?)",
                (action_id, path, operation, expected_hash, before_content, after_content, diff),
            )
        return self.get_change(action_id, session_id=session_id)  # type: ignore[return-value]

    def get_change(self, change_id: str, *, session_id: str | None = None) -> ChangeInfo | None:
        query = "SELECT a.*,d.* FROM actions a JOIN file_action_data d ON d.action_id=a.id WHERE a.id=?"
        values: list[Any] = [change_id]
        if session_id is not None:
            query += " AND a.session_id=?"
            values.append(session_id)
        row = self.connection.execute(query, values).fetchone()
        return None if row is None else _change_info(row)

    def list_changes(self, session_id: str, *, statuses: tuple[str, ...] | None = None) -> list[ChangeInfo]:
        query = "SELECT a.*,d.* FROM actions a JOIN file_action_data d ON d.action_id=a.id WHERE a.session_id=?"
        values: list[Any] = [session_id]
        if statuses:
            normalized = [_normalize_status(item)[0].value for item in statuses]
            query += " AND a.status IN (" + ",".join("?" for _ in normalized) + ")"
            values.extend(normalized)
        query += " ORDER BY a.created_at"
        return [_change_info(row) for row in self.connection.execute(query, values).fetchall()]

    def update_change_status(self, change_id: str, status: str, *, error: str | None = None) -> None:
        normalized, result = _normalize_status(status, error=error)
        fields = ["status=?", "error=?"]
        values: list[Any] = [normalized.value, error]
        timestamp = now()
        if normalized is ActionStatus.APPROVED:
            fields.append("approved_at=?")
            values.append(timestamp)
        elif normalized is ActionStatus.RUNNING:
            fields.append("started_at=?")
            values.append(timestamp)
        elif status == "applied":
            fields.extend(["started_at=?", "finished_at=?", "result_kind=?"])
            values.extend([timestamp, timestamp, ActionResultKind.APPLIED.value])
        elif status == "undone":
            fields.extend(["reversed_at=?", "result_kind=?"])
            values.extend([timestamp, ActionResultKind.UNDONE.value])
        elif normalized in {ActionStatus.FAILED, ActionStatus.CANCELLED, ActionStatus.REJECTED}:
            fields.append("finished_at=?")
            values.append(timestamp)
            if result is not None:
                fields.append("result_kind=?")
                values.append(result.value)
        values.append(change_id)
        self.connection.execute(f"UPDATE actions SET {','.join(fields)} WHERE id=?", values)
        self.connection.commit()

    def last_applied_change(self, session_id: str) -> ChangeInfo | None:
        row = self.connection.execute(
            """SELECT a.*,d.* FROM actions a JOIN file_action_data d ON d.action_id=a.id
               WHERE a.session_id=? AND a.status='completed' AND a.result_kind='applied'
               ORDER BY a.finished_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        return None if row is None else _change_info(row)

    def create_command(self, *, session_id: str, run_id: str, template: str, argv: list[str], cwd: str, timeout_seconds: float, summary: str) -> CommandInfo:
        action_id, created = uuid.uuid4().hex, now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO actions(id,session_id,run_id,action_type,status,summary,created_at) VALUES(?,?,?,?,?,?,?)",
                (action_id, session_id, run_id, ActionType.COMMAND.value, ActionStatus.PENDING.value, summary, created),
            )
            self.connection.execute(
                "INSERT INTO command_action_data(action_id,template,argv,cwd,timeout_seconds) VALUES(?,?,?,?,?)",
                (action_id, template, json.dumps(argv), cwd, timeout_seconds),
            )
        return self.get_command(action_id, session_id=session_id)  # type: ignore[return-value]

    def get_command(self, command_id: str, *, session_id: str | None = None) -> CommandInfo | None:
        query = "SELECT a.*,d.* FROM actions a JOIN command_action_data d ON d.action_id=a.id WHERE a.id=?"
        values: list[Any] = [command_id]
        if session_id is not None:
            query += " AND a.session_id=?"
            values.append(session_id)
        row = self.connection.execute(query, values).fetchone()
        return None if row is None else _command_info(row)

    def list_commands(self, session_id: str) -> list[CommandInfo]:
        rows = self.connection.execute(
            "SELECT a.*,d.* FROM actions a JOIN command_action_data d ON d.action_id=a.id WHERE a.session_id=? ORDER BY a.created_at",
            (session_id,),
        ).fetchall()
        return [_command_info(row) for row in rows]

    def update_command(self, command_id: str, status: str, *, exit_code: int | None = None, stdout: str = "", stderr: str = "", truncated: bool = False, error: str | None = None) -> None:
        normalized, result = _normalize_status(status, error=error, exit_code=exit_code)
        timestamp = now()
        fields = ["status=?", "error=?"]
        values: list[Any] = [normalized.value, error]
        if normalized is ActionStatus.APPROVED:
            fields.append("approved_at=?")
            values.append(timestamp)
        elif normalized is ActionStatus.RUNNING:
            fields.append("started_at=?")
            values.append(timestamp)
        elif normalized in {ActionStatus.COMPLETED, ActionStatus.FAILED, ActionStatus.CANCELLED}:
            fields.extend(["finished_at=?", "result_kind=?"])
            values.extend([timestamp, result.value if result else None])
        values.append(command_id)
        with self.connection:
            self.connection.execute(f"UPDATE actions SET {','.join(fields)} WHERE id=?", values)
            self.connection.execute(
                "UPDATE command_action_data SET exit_code=?,stdout=?,stderr=?,output_truncated=? WHERE action_id=?",
                (exit_code, stdout, stderr, int(truncated), command_id),
            )

    def create_external_action(self, *, session_id: str, run_id: str, kind: str, payload: dict[str, Any], summary: str) -> ExternalActionInfo:
        action_id, created = uuid.uuid4().hex, now()
        action_type = ActionType(kind)
        with self.connection:
            self.connection.execute(
                "INSERT INTO actions(id,session_id,run_id,action_type,status,summary,created_at) VALUES(?,?,?,?,?,?,?)",
                (action_id, session_id, run_id, action_type.value, ActionStatus.PENDING.value, summary, created),
            )
            self.connection.execute(
                "INSERT INTO external_action_data(action_id,payload) VALUES(?,?)",
                (action_id, json.dumps(payload, ensure_ascii=False)),
            )
        return self.get_external_action(action_id, session_id=session_id)  # type: ignore[return-value]

    def get_external_action(self, action_id: str, *, session_id: str | None = None) -> ExternalActionInfo | None:
        query = "SELECT a.*,d.* FROM actions a JOIN external_action_data d ON d.action_id=a.id WHERE a.id=?"
        values: list[Any] = [action_id]
        if session_id is not None:
            query += " AND a.session_id=?"
            values.append(session_id)
        row = self.connection.execute(query, values).fetchone()
        return None if row is None else _external_info(row)

    def list_external_actions(self, session_id: str) -> list[ExternalActionInfo]:
        rows = self.connection.execute(
            "SELECT a.*,d.* FROM actions a JOIN external_action_data d ON d.action_id=a.id WHERE a.session_id=? ORDER BY a.created_at",
            (session_id,),
        ).fetchall()
        return [_external_info(row) for row in rows]

    def update_external_action(self, action_id: str, status: str, *, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        normalized, result_kind = _normalize_status(status, error=error, external=True)
        timestamp = now()
        fields = ["status=?", "error=?"]
        values: list[Any] = [normalized.value, error]
        if normalized is ActionStatus.APPROVED:
            fields.append("approved_at=?")
            values.append(timestamp)
        elif normalized is ActionStatus.RUNNING:
            fields.append("started_at=?")
            values.append(timestamp)
        elif normalized in {ActionStatus.COMPLETED, ActionStatus.FAILED, ActionStatus.CANCELLED, ActionStatus.REJECTED}:
            fields.extend(["finished_at=?", "result_kind=?"])
            values.extend([timestamp, result_kind.value if result_kind else None])
        values.append(action_id)
        with self.connection:
            self.connection.execute(f"UPDATE actions SET {','.join(fields)} WHERE id=?", values)
            self.connection.execute(
                "UPDATE external_action_data SET result=? WHERE action_id=?",
                (json.dumps(result, ensure_ascii=False) if result is not None else None, action_id),
            )

    def get_action(self, action_id: str, *, session_id: str | None = None) -> ActionInfo | None:
        query, values = "SELECT * FROM actions WHERE id=?", [action_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = self.connection.execute(query, values).fetchone()
        return None if row is None else _action_info(row)

    def list_actions(self, session_id: str, *, types: set[ActionType] | None = None) -> list[ActionInfo]:
        query, values = "SELECT * FROM actions WHERE session_id=?", [session_id]
        if types:
            query += " AND action_type IN (" + ",".join("?" for _ in types) + ")"
            values.extend(item.value for item in types)
        query += " ORDER BY created_at"
        return [_action_info(row) for row in self.connection.execute(query, values).fetchall()]

    def resolve_action(self, session_id: str, prefix: str, *, types: set[ActionType] | None = None) -> ActionInfo | None:
        exact = self.get_action(prefix, session_id=session_id)
        if exact is not None and (types is None or exact.type in types):
            return exact
        matches = [item for item in self.list_actions(session_id, types=types) if item.id.startswith(prefix)]
        if len(matches) > 1:
            raise ValueError("action id prefix is ambiguous; provide more characters")
        return matches[0] if matches else None


class TaskRepository(Repository):
    def replace_tasks(self, session_id: str, items: list[str]) -> list[TaskInfo]:
        timestamp = now()
        existing = {row["text"]: row for row in self.connection.execute("SELECT * FROM tasks WHERE session_id=? AND status IN ('pending','running','blocked')", (session_id,))}
        with self.connection:
            for text in items:
                if text not in existing:
                    self.connection.execute("INSERT INTO tasks(id,session_id,text,status,created_at,updated_at) VALUES(?,?,?,'pending',?,?)", (uuid.uuid4().hex, session_id, text, timestamp, timestamp))
            for text, row in existing.items():
                if text not in items:
                    self.connection.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (timestamp, row["id"]))
        return self.list_tasks(session_id)

    def list_tasks(self, session_id: str) -> list[TaskInfo]:
        return [TaskInfo(row["id"], row["session_id"], row["text"], row["status"]) for row in self.connection.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY created_at", (session_id,))]

    def update_task_status(self, task_id: str, session_id: str, status: str) -> TaskInfo:
        if status not in {"pending", "running", "blocked", "completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported task status: {status}")
        updated = self.connection.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=? AND session_id=?", (status, now(), task_id, session_id)).rowcount
        if not updated:
            raise ValueError("task does not belong to this session or does not exist")
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return TaskInfo(row["id"], row["session_id"], row["text"], row["status"])


class SettingsRepository(Repository):
    def workspace_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute("SELECT value FROM workspace_settings WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row[0])

    def set_workspace_setting(self, key: str, value: str) -> None:
        self.connection.execute("INSERT INTO workspace_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.connection.commit()


class SourceRepository(Repository):
    def add_source(self, *, session_id: str, run_id: str, url: str, title: str, excerpt: str, suspicious: bool = False) -> SourceInfo:
        source_id, fetched = uuid.uuid4().hex, now()
        self.connection.execute("INSERT INTO sources(id,session_id,run_id,url,title,excerpt,fetched_at,suspicious) VALUES(?,?,?,?,?,?,?,?)", (source_id, session_id, run_id, url, title, excerpt, fetched, int(suspicious)))
        self.connection.commit()
        return self.get_source(source_id, session_id=session_id)  # type: ignore[return-value]

    def get_source(self, source_id: str, *, session_id: str | None = None) -> SourceInfo | None:
        query, values = "SELECT * FROM sources WHERE id=?", [source_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = self.connection.execute(query, values).fetchone()
        return None if row is None else _source_info(row)

    def list_sources(self, session_id: str) -> list[SourceInfo]:
        return [_source_info(row) for row in self.connection.execute("SELECT * FROM sources WHERE session_id=? ORDER BY fetched_at", (session_id,)).fetchall()]


def _normalize_status(status: str, *, error: str | None = None, exit_code: int | None = None, external: bool = False) -> tuple[ActionStatus, ActionResultKind | None]:
    if status == "discarded":
        return ActionStatus.REJECTED, None
    if status == "applied":
        return ActionStatus.COMPLETED, ActionResultKind.APPLIED
    if status == "undone":
        return ActionStatus.COMPLETED, ActionResultKind.UNDONE
    normalized = ActionStatus(status)
    if normalized is ActionStatus.COMPLETED:
        return normalized, ActionResultKind.SUCCESS if external else ActionResultKind.EXIT_ZERO
    if normalized is ActionStatus.CANCELLED:
        return normalized, ActionResultKind.USER_CANCELLED
    if normalized is ActionStatus.FAILED:
        if error and "timed out" in error.casefold():
            return normalized, ActionResultKind.TIMEOUT
        if exit_code is not None:
            return normalized, ActionResultKind.NONZERO_EXIT
        return normalized, ActionResultKind.EXECUTION_ERROR
    return normalized, None


def _session_info(row: sqlite3.Row) -> SessionInfo:
    return SessionInfo(row["id"], Path(row["workspace"]), row["model"], row["created_at"], row["updated_at"])


def _action_info(row: sqlite3.Row) -> ActionInfo:
    return ActionInfo(
        row["id"], row["session_id"], row["run_id"], ActionType(row["action_type"]),
        ActionStatus(row["status"]), ActionResultKind(row["result_kind"]) if row["result_kind"] else None,
        row["summary"], row["created_at"], row["approved_at"], row["started_at"],
        row["finished_at"], row["reversed_at"], row["error"],
    )


def _change_info(row: sqlite3.Row) -> ChangeInfo:
    action = _action_info(row)
    return ChangeInfo(action.id, action.session_id, action.run_id, row["path"], row["operation"], row["expected_hash"], row["before_content"], row["after_content"], row["diff"], action.summary, action.status, action.created_at, action.result_kind, action.error)


def _command_info(row: sqlite3.Row) -> CommandInfo:
    action = _action_info(row)
    return CommandInfo(action.id, action.session_id, action.run_id, row["template"], tuple(json.loads(row["argv"])), row["cwd"], row["timeout_seconds"], action.summary, action.status, row["exit_code"], row["stdout"], row["stderr"], action.result_kind, action.error)


def _external_info(row: sqlite3.Row) -> ExternalActionInfo:
    action = _action_info(row)
    return ExternalActionInfo(action.id, action.session_id, action.run_id, action.type.value, json.loads(row["payload"]), action.summary, action.status, json.loads(row["result"]) if row["result"] else None, action.error, action.result_kind)


def _source_info(row: sqlite3.Row) -> SourceInfo:
    return SourceInfo(row["id"], row["session_id"], row["run_id"], row["url"], row["title"], row["excerpt"], row["fetched_at"], bool(row["suspicious"]))
