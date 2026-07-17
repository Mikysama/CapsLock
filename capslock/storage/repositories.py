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
    AgentEvent,
    AgentEventKind,
    ChangeInfo,
    CommandInfo,
    ExternalActionInfo,
    SessionInfo,
    SessionTitleSource,
    RunStepInfo,
    RunStepKind,
    RunStepStatus,
    SourceInfo,
    TaskInfo,
    WorkItemInfo,
    WorkItemStatus,
    normalize_session_title,
    pending_session_title,
)


def now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


class SessionRepository(Repository):
    def create(self, workspace: Path, model: str) -> SessionInfo:
        session_id, created = uuid.uuid4().hex, now()
        title = pending_session_title(created)
        self.connection.execute(
            """INSERT INTO sessions(
                 id,workspace,model,created_at,updated_at,title,title_source,title_updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                session_id,
                str(workspace.resolve()),
                model,
                created,
                created,
                title,
                SessionTitleSource.PENDING.value,
                created,
            ),
        )
        self.connection.execute(
            "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
            (session_id, "title", title, created),
        )
        self.connection.commit()
        return SessionInfo(
            session_id,
            workspace.resolve(),
            model,
            created,
            created,
            title,
            SessionTitleSource.PENDING,
            created,
        )

    def get(self, session_id: str) -> SessionInfo | None:
        row = self.connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return None if row is None else _session_info(row)

    def list(self, limit: int = 20, *, include_archived: bool = False) -> list[SessionInfo]:
        query = "SELECT * FROM sessions WHERE deletion_state IS NULL"
        if not include_archived:
            query += " AND archived_at IS NULL"
        query += " ORDER BY updated_at DESC LIMIT ?"
        rows = self.connection.execute(query, (limit,)).fetchall()
        return [_session_info(row) for row in rows]

    def search_sessions(self, query: str, *, limit: int = 20, include_archived: bool = False) -> list[SessionInfo]:
        normalized = query.strip()
        if not normalized:
            return self.list(limit, include_archived=include_archived)
        sql = """SELECT s.*,min(bm25(session_search)) AS rank
                 FROM session_search JOIN sessions s ON s.id=session_search.session_id
                 WHERE session_search MATCH ? AND s.deletion_state IS NULL"""
        values: list[Any] = [normalized]
        if not include_archived:
            sql += " AND s.archived_at IS NULL"
        sql += " GROUP BY s.id ORDER BY rank,s.updated_at DESC LIMIT ?"
        values.append(limit)
        try:
            rows = self.connection.execute(sql, values).fetchall()
        except sqlite3.OperationalError:
            escaped = normalized.replace("%", "\\%").replace("_", "\\_")
            sql = """SELECT DISTINCT s.* FROM sessions s LEFT JOIN messages m ON m.session_id=s.id
                     WHERE s.deletion_state IS NULL AND (s.title LIKE ? ESCAPE '\\' OR m.content LIKE ? ESCAPE '\\')"""
            fallback: list[Any] = [f"%{escaped}%", f"%{escaped}%"]
            if not include_archived:
                sql += " AND s.archived_at IS NULL"
            sql += " ORDER BY s.updated_at DESC LIMIT ?"
            fallback.append(limit)
            rows = self.connection.execute(sql, fallback).fetchall()
        return [_session_info(row) for row in rows]

    def resolve_session(self, prefix: str) -> SessionInfo | None:
        normalized = prefix.strip()
        if not normalized:
            raise ValueError("session id cannot be empty")
        exact = self.get(normalized)
        if exact is not None:
            return exact
        rows = self.connection.execute(
            "SELECT * FROM sessions WHERE substr(id,1,?)=? ORDER BY updated_at DESC LIMIT 2",
            (len(normalized), normalized),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"session id prefix is ambiguous: {normalized}")
        return _session_info(rows[0]) if rows else None

    def rename_session(self, session_id: str, title: str) -> SessionInfo:
        normalized = normalize_session_title(title)
        timestamp = now()
        with self.connection:
            updated = self.connection.execute(
                """UPDATE sessions
                   SET title=?,title_source=?,title_updated_at=?
                   WHERE id=?""",
                (normalized, SessionTitleSource.MANUAL.value, timestamp, session_id),
            ).rowcount
            if updated:
                self.connection.execute(
                    "DELETE FROM session_search WHERE session_id=? AND kind='title'", (session_id,)
                )
                self.connection.execute(
                    "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
                    (session_id, "title", normalized, timestamp),
                )
        if not updated:
            raise ValueError(f"session does not exist: {session_id}")
        return self.get(session_id)  # type: ignore[return-value]

    def delete_session_if_empty(self, session_id: str) -> bool:
        with self.connection:
            deleted = self.connection.execute(
                """DELETE FROM sessions
                   WHERE id=? AND title_source='pending'
                     AND NOT EXISTS (SELECT 1 FROM runs WHERE session_id=sessions.id)
                     AND NOT EXISTS (SELECT 1 FROM messages WHERE session_id=sessions.id)
                     AND NOT EXISTS (SELECT 1 FROM actions WHERE session_id=sessions.id)
                     AND NOT EXISTS (SELECT 1 FROM tasks WHERE session_id=sessions.id)
                     AND NOT EXISTS (SELECT 1 FROM work_items WHERE session_id=sessions.id)
                     AND NOT EXISTS (SELECT 1 FROM sources WHERE session_id=sessions.id)""",
                (session_id,),
            ).rowcount
        return bool(deleted)

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

    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        """Return all visible conversation state, including interrupted runs."""
        entries: list[tuple[str, int, dict[str, Any]]] = []
        message_runs: set[str] = set()
        for row in self.connection.execute(
            "SELECT id,role,content,created_at,run_id FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        ):
            if row["run_id"]:
                message_runs.add(str(row["run_id"]))
            entries.append(
                (
                    str(row["created_at"]),
                    int(row["id"]) * 2,
                    {
                        "role": str(row["role"]),
                        "content": str(row["content"]),
                        "run_id": str(row["run_id"]) if row["run_id"] else None,
                        "status": "completed",
                    },
                )
            )

        runs = self.connection.execute(
            "SELECT id,question,status,started_at,error FROM runs WHERE session_id=? ORDER BY started_at",
            (session_id,),
        ).fetchall()
        for index, row in enumerate(runs):
            run_id = str(row["id"])
            if run_id in message_runs:
                continue
            timestamp = str(row["started_at"])
            status = str(row["status"])
            entries.append(
                (
                    timestamp,
                    index * 2,
                    {
                        "role": "user",
                        "content": str(row["question"]),
                        "run_id": run_id,
                        "status": status,
                    },
                )
            )
            text = "".join(
                str(json.loads(event["payload"]).get("text", ""))
                for event in self.connection.execute(
                    "SELECT payload FROM run_events WHERE run_id=? AND event_kind=? ORDER BY sequence",
                    (run_id, AgentEventKind.TEXT_DELTA.value),
                )
            )
            entries.append(
                (
                    timestamp,
                    index * 2 + 1,
                    {
                        "role": "assistant",
                        "content": text,
                        "run_id": run_id,
                        "status": status,
                        "error": str(row["error"]) if row["error"] else None,
                    },
                )
            )
        entries.sort(key=lambda item: (item[0], item[1]))
        return [entry for _, _, entry in entries]

    def append_message(self, session_id: str, role: str, content: str, *, run_id: str | None = None) -> None:
        timestamp = now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO messages(session_id,role,content,created_at,run_id) VALUES(?,?,?,?,?)",
                (session_id, role, content, timestamp, run_id),
            )
            self.connection.execute(
                "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
                (session_id, "message", content, timestamp),
            )
            self.connection.execute("UPDATE sessions SET updated_at=? WHERE id=?", (timestamp, session_id))

    def archive_session(self, session_id: str, *, archived: bool = True) -> SessionInfo:
        timestamp = now() if archived else None
        updated = self.connection.execute(
            "UPDATE sessions SET archived_at=?,updated_at=? WHERE id=? AND deletion_state IS NULL",
            (timestamp, now(), session_id),
        ).rowcount
        self.connection.commit()
        if not updated:
            raise ValueError(f"session does not exist: {session_id}")
        return self.get(session_id)  # type: ignore[return-value]

    def mark_session_deleting(self, session_id: str) -> None:
        updated = self.connection.execute(
            "UPDATE sessions SET deletion_state='deleting',updated_at=? WHERE id=?",
            (now(), session_id),
        ).rowcount
        self.connection.commit()
        if not updated:
            raise ValueError(f"session does not exist: {session_id}")

    def delete_session(self, session_id: str) -> None:
        with self.connection:
            run_ids = [row[0] for row in self.connection.execute("SELECT id FROM runs WHERE session_id=?", (session_id,))]
            action_ids = [row[0] for row in self.connection.execute("SELECT id FROM actions WHERE session_id=?", (session_id,))]
            if run_ids:
                marks = ",".join("?" for _ in run_ids)
                for table in ("run_events", "run_steps", "tool_calls", "citations"):
                    self.connection.execute(f"DELETE FROM {table} WHERE run_id IN ({marks})", run_ids)
            if action_ids:
                marks = ",".join("?" for _ in action_ids)
                for table in ("file_action_data", "command_action_data", "external_action_data"):
                    self.connection.execute(f"DELETE FROM {table} WHERE action_id IN ({marks})", action_ids)
            for table in ("work_items", "tasks", "sources", "actions", "messages", "runs"):
                self.connection.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
            self.connection.execute("DELETE FROM session_search WHERE session_id=?", (session_id,))
            self.connection.execute("DELETE FROM sessions WHERE id=?", (session_id,))

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
    def run_completed(self, run_id: str, *, session_id: str | None = None) -> bool:
        query = "SELECT status FROM runs WHERE id=?"
        values: list[object] = [run_id]
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        row = self.connection.execute(query, values).fetchone()
        return row is not None and str(row[0]) == "completed"

    def start_run(
        self,
        session_id: str,
        question: str,
        *,
        work_item_id: str | None = None,
        parent_run_id: str | None = None,
        resume_from_step_id: str | None = None,
    ) -> str:
        run_id, started = uuid.uuid4().hex, now()
        try:
            title = normalize_session_title(question, truncate=True)
        except ValueError:
            title = None
        with self.connection:
            self.connection.execute(
                """INSERT INTO runs(
                     id,session_id,question,status,started_at,work_item_id,parent_run_id,resume_from_step_id
                   ) VALUES(?,?,?,'running',?,?,?,?)""",
                (run_id, session_id, question, started, work_item_id, parent_run_id, resume_from_step_id),
            )
            if title:
                self.connection.execute(
                    """UPDATE sessions
                       SET title=?,title_source=?,title_updated_at=?
                       WHERE id=? AND title_source=?""",
                    (
                        title,
                        SessionTitleSource.FIRST_QUESTION.value,
                        started,
                        session_id,
                        SessionTitleSource.PENDING.value,
                    ),
                )
            if work_item_id is not None:
                self.connection.execute(
                    "UPDATE work_items SET status='running',current_run_id=?,updated_at=? WHERE id=?",
                    (run_id, started, work_item_id),
                )
        return run_id

    def finish_run(self, run_id: str, *, status: str, duration_ms: int, error: str | None = None, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0) -> None:
        self.connection.execute(
            "UPDATE runs SET status=?,finished_at=?,duration_ms=?,error=?,input_tokens=?,output_tokens=?,cost_usd=? WHERE id=?",
            (status, now(), duration_ms, error, input_tokens, output_tokens, cost_usd, run_id),
        )
        self.connection.commit()

    def complete_waiting_run(self, run_id: str) -> None:
        self.connection.execute(
            "UPDATE runs SET status='completed',finished_at=coalesce(finished_at,?) WHERE id=? AND status='waiting_approval'",
            (now(), run_id),
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
    def set_action_risk(self, action_id: str, *, level: str, reason: str, rollback: str) -> None:
        self.connection.execute(
            "UPDATE actions SET risk_level=?,risk_reason=?,rollback=? WHERE id=?",
            (level, reason, rollback, action_id),
        )
        self.connection.commit()

    def cancel_run_actions(self, run_id: str, *, error: str) -> int:
        timestamp = now()
        with self.connection:
            updated = self.connection.execute(
                """UPDATE actions SET status='cancelled',result_kind='user_cancelled',
                     finished_at=?,error=?
                   WHERE run_id=? AND status IN ('pending','approved','running')""",
                (timestamp, error, run_id),
            ).rowcount
        return int(updated)

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
            fields.extend(["approved_at=?", "decided_at=?"])
            values.extend([timestamp, timestamp])
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
            if normalized is ActionStatus.REJECTED:
                fields.append("decided_at=?")
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
            fields.extend(["approved_at=?", "decided_at=?"])
            values.extend([timestamp, timestamp])
        elif normalized is ActionStatus.RUNNING:
            fields.append("started_at=?")
            values.append(timestamp)
        elif normalized in {ActionStatus.COMPLETED, ActionStatus.FAILED, ActionStatus.CANCELLED, ActionStatus.REJECTED}:
            fields.extend(["finished_at=?", "result_kind=?"])
            values.extend([timestamp, result.value if result else None])
            if normalized is ActionStatus.REJECTED:
                fields.append("decided_at=?")
                values.append(timestamp)
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
            fields.extend(["approved_at=?", "decided_at=?"])
            values.extend([timestamp, timestamp])
        elif normalized is ActionStatus.RUNNING:
            fields.append("started_at=?")
            values.append(timestamp)
        elif normalized in {ActionStatus.COMPLETED, ActionStatus.FAILED, ActionStatus.CANCELLED, ActionStatus.REJECTED}:
            fields.extend(["finished_at=?", "result_kind=?"])
            values.extend([timestamp, result_kind.value if result_kind else None])
            if normalized is ActionStatus.REJECTED:
                fields.append("decided_at=?")
                values.append(timestamp)
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
    def replace_tasks(self, session_id: str, items: list[str], *, run_id: str | None = None) -> list[TaskInfo]:
        timestamp = now()
        existing = {row["text"]: row for row in self.connection.execute("SELECT * FROM tasks WHERE session_id=? AND run_id IS ? AND status IN ('pending','running','blocked')", (session_id, run_id))}
        with self.connection:
            for position, text in enumerate(items):
                if text not in existing:
                    self.connection.execute("INSERT INTO tasks(id,session_id,text,status,created_at,updated_at,run_id,position) VALUES(?,?,?,'pending',?,?,?,?)", (uuid.uuid4().hex, session_id, text, timestamp, timestamp, run_id, position))
                else:
                    self.connection.execute("UPDATE tasks SET position=?,updated_at=? WHERE id=?", (position, timestamp, existing[text]["id"]))
            for text, row in existing.items():
                if text not in items:
                    self.connection.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (timestamp, row["id"]))
        return self.list_tasks(session_id, run_id=run_id)

    def list_tasks(self, session_id: str, *, run_id: str | None = None) -> list[TaskInfo]:
        query = "SELECT * FROM tasks WHERE session_id=?"
        values: list[Any] = [session_id]
        if run_id is not None:
            query += " AND run_id=?"
            values.append(run_id)
        query += " ORDER BY position,created_at"
        return [_task_info(row) for row in self.connection.execute(query, values)]

    def update_task_status(self, task_id: str, session_id: str, status: str) -> TaskInfo:
        if status not in {"pending", "running", "blocked", "completed", "failed", "cancelled"}:
            raise ValueError(f"unsupported task status: {status}")
        updated = self.connection.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=? AND session_id=?", (status, now(), task_id, session_id)).rowcount
        if not updated:
            raise ValueError("task does not belong to this session or does not exist")
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _task_info(row)


class WorkflowRepository(Repository):
    def enqueue_work_item(self, session_id: str, question: str, *, parent_work_item_id: str | None = None) -> WorkItemInfo:
        timestamp, item_id = now(), uuid.uuid4().hex
        position = int(self.connection.execute(
            "SELECT coalesce(max(position),-1)+1 FROM work_items WHERE session_id=? AND status='queued'",
            (session_id,),
        ).fetchone()[0])
        self.connection.execute(
            """INSERT INTO work_items(
                 id,session_id,question,status,position,parent_work_item_id,created_at,updated_at
               ) VALUES(?,?,?,'queued',?,?,?,?)""",
            (item_id, session_id, question, position, parent_work_item_id, timestamp, timestamp),
        )
        self.connection.commit()
        return self.get_work_item(item_id)  # type: ignore[return-value]

    def get_work_item(self, item_id: str) -> WorkItemInfo | None:
        row = self.connection.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
        return None if row is None else _work_item_info(row)

    def list_work_items(self, session_id: str, *, active_only: bool = False) -> list[WorkItemInfo]:
        query = "SELECT * FROM work_items WHERE session_id=?"
        values: list[Any] = [session_id]
        if active_only:
            query += " AND status IN ('queued','running','waiting_approval','interrupted')"
        query += " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'waiting_approval' THEN 1 ELSE 2 END,position,created_at"
        return [_work_item_info(row) for row in self.connection.execute(query, values)]

    def update_work_item(self, item_id: str, status: WorkItemStatus | str, *, error: str | None = None) -> WorkItemInfo:
        normalized = WorkItemStatus(status)
        updated = self.connection.execute(
            "UPDATE work_items SET status=?,error=?,updated_at=? WHERE id=?",
            (normalized.value, error, now(), item_id),
        ).rowcount
        self.connection.commit()
        if not updated:
            raise ValueError(f"work item does not exist: {item_id}")
        return self.get_work_item(item_id)  # type: ignore[return-value]

    def reorder_work_item(self, item_id: str, position: int) -> WorkItemInfo:
        item = self.get_work_item(item_id)
        if item is None or item.status is not WorkItemStatus.QUEUED:
            raise ValueError("only queued work items can be reordered")
        with self.connection:
            self.connection.execute(
                "UPDATE work_items SET position=position+1 WHERE session_id=? AND status='queued' AND position>=?",
                (item.session_id, max(0, position)),
            )
            self.connection.execute(
                "UPDATE work_items SET position=?,updated_at=? WHERE id=?",
                (max(0, position), now(), item_id),
            )
        return self.get_work_item(item_id)  # type: ignore[return-value]

    def create_run_step(self, run_id: str, kind: RunStepKind | str) -> RunStepInfo:
        ordinal = int(self.connection.execute(
            "SELECT coalesce(max(ordinal),-1)+1 FROM run_steps WHERE run_id=?", (run_id,)
        ).fetchone()[0])
        step_id, timestamp = uuid.uuid4().hex, now()
        self.connection.execute(
            "INSERT INTO run_steps(id,run_id,ordinal,kind,status,started_at) VALUES(?,?,?,?,?,?)",
            (step_id, run_id, ordinal, RunStepKind(kind).value, RunStepStatus.RUNNING.value, timestamp),
        )
        self.connection.commit()
        return self.get_run_step(step_id)  # type: ignore[return-value]

    def finish_run_step(self, step_id: str, *, status: RunStepStatus | str, checkpoint: dict[str, Any] | None = None, error: str | None = None) -> RunStepInfo:
        self.connection.execute(
            "UPDATE run_steps SET status=?,checkpoint=?,finished_at=?,error=? WHERE id=?",
            (RunStepStatus(status).value, json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None, now(), error, step_id),
        )
        self.connection.commit()
        result = self.get_run_step(step_id)
        if result is None:
            raise ValueError(f"run step does not exist: {step_id}")
        return result

    def get_run_step(self, step_id: str) -> RunStepInfo | None:
        row = self.connection.execute("SELECT * FROM run_steps WHERE id=?", (step_id,)).fetchone()
        return None if row is None else _run_step_info(row)

    def last_stable_step(self, run_id: str) -> RunStepInfo | None:
        row = self.connection.execute(
            "SELECT * FROM run_steps WHERE run_id=? AND status='completed' AND checkpoint IS NOT NULL ORDER BY ordinal DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else _run_step_info(row)

    def append_run_event(self, *, session_id: str, run_id: str, work_item_id: str, kind: AgentEventKind | str, payload: dict[str, Any]) -> AgentEvent:
        sequence = int(self.connection.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM run_events WHERE run_id=?", (run_id,)
        ).fetchone()[0])
        timestamp = now()
        self.connection.execute(
            "INSERT INTO run_events(run_id,work_item_id,session_id,sequence,event_kind,payload,created_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, work_item_id, session_id, sequence, AgentEventKind(kind).value, json.dumps(payload, ensure_ascii=False), timestamp),
        )
        self.connection.commit()
        return AgentEvent(sequence, timestamp, session_id, run_id, work_item_id, AgentEventKind(kind), payload)

    def run_events(self, run_id: str) -> list[AgentEvent]:
        rows = self.connection.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY sequence", (run_id,))
        return [_agent_event(row) for row in rows]


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


class SkillSettingsRepository(Repository):
    def skill_enabled(self, name: str) -> bool:
        row = self.connection.execute(
            "SELECT enabled FROM skill_settings WHERE name=?", (name,)
        ).fetchone()
        return row is None or bool(row[0])

    def set_skill_enabled(self, name: str, enabled: bool) -> None:
        self.connection.execute(
            """INSERT INTO skill_settings(name,enabled,updated_at) VALUES(?,?,?)
               ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
            (name, int(enabled), now()),
        )
        self.connection.commit()

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
    return SessionInfo(
        row["id"],
        Path(row["workspace"]),
        row["model"],
        row["created_at"],
        row["updated_at"],
        row["title"],
        SessionTitleSource(row["title_source"]),
        row["title_updated_at"],
        row["archived_at"],
        row["deletion_state"],
    )


def _action_info(row: sqlite3.Row) -> ActionInfo:
    return ActionInfo(
        row["id"], row["session_id"], row["run_id"], ActionType(row["action_type"]),
        ActionStatus(row["status"]), ActionResultKind(row["result_kind"]) if row["result_kind"] else None,
        row["summary"], row["created_at"], row["approved_at"], row["started_at"],
        row["finished_at"], row["reversed_at"], row["error"], row["risk_level"],
        row["risk_reason"], row["rollback"], row["decided_at"],
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


def _task_info(row: sqlite3.Row) -> TaskInfo:
    return TaskInfo(row["id"], row["session_id"], row["text"], row["status"], row["run_id"], int(row["position"]))


def _work_item_info(row: sqlite3.Row) -> WorkItemInfo:
    return WorkItemInfo(
        row["id"], row["session_id"], row["question"], WorkItemStatus(row["status"]),
        int(row["position"]), row["created_at"], row["updated_at"], row["current_run_id"],
        row["parent_work_item_id"], row["error"],
    )


def _run_step_info(row: sqlite3.Row) -> RunStepInfo:
    return RunStepInfo(
        row["id"], row["run_id"], int(row["ordinal"]), RunStepKind(row["kind"]),
        RunStepStatus(row["status"]), json.loads(row["checkpoint"]) if row["checkpoint"] else None,
        row["started_at"], row["finished_at"], row["error"],
    )


def _agent_event(row: sqlite3.Row) -> AgentEvent:
    return AgentEvent(
        int(row["sequence"]), row["created_at"], row["session_id"], row["run_id"],
        row["work_item_id"], AgentEventKind(row["event_kind"]), json.loads(row["payload"]),
    )
