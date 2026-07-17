"""Search, archive, export, and permanently delete workspace sessions."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .security import redact
from .session import SessionStore
from .storage import MemoryStore, workspace_key


SESSION_EXPORT_FORMAT = "capslock-session-export"
SESSION_EXPORT_VERSION = 1


class SessionManager:
    def __init__(
        self,
        store: SessionStore,
        *,
        workspace: Path,
        memory_store: MemoryStore | None = None,
    ) -> None:
        self.store = store
        self.workspace = workspace.resolve()
        self.memory_store = memory_store

    def export(self, session_id: str, destination: str | Path) -> Path:
        session = self.store.get(session_id)
        if session is None:
            raise ValueError(f"session does not exist: {session_id}")
        target = self._export_path(destination)
        if target.exists():
            raise FileExistsError(f"export destination already exists: {target}")
        document = self._document(session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
        try:
            (temporary / "session.json").write_text(
                json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            (temporary / "session.md").write_text(self._markdown(document), encoding="utf-8")
            os.replace(temporary, target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return target

    def delete(self, session_id: str) -> int:
        session = self.store.get(session_id)
        if session is None:
            raise ValueError(f"session does not exist: {session_id}")
        active = self.store._connection.execute(
            "SELECT 1 FROM work_items WHERE session_id=? AND status IN ('running','waiting_approval') LIMIT 1",
            (session_id,),
        ).fetchone()
        if active:
            raise ValueError("active or approval-waiting sessions must be cancelled before deletion")
        self.store.mark_session_deleting(session_id)
        purged = 0
        if self.memory_store is not None:
            purged = self.memory_store.purge_session(
                workspace=workspace_key(self.workspace),
                session_id=session_id,
            )
        self.store.delete_session(session_id)
        return purged

    def _export_path(self, destination: str | Path) -> Path:
        candidate = Path(destination)
        if candidate.is_absolute():
            raise ValueError("session export destination must be workspace-relative")
        target = (self.workspace / candidate).resolve()
        if not target.is_relative_to(self.workspace):
            raise ValueError("session export destination escapes the workspace")
        current = self.workspace
        for part in candidate.parts:
            if part in {"", "."}:
                continue
            current = current / part
            if current.exists() and current.is_symlink():
                raise ValueError("session export does not follow symbolic links")
        return target

    def _document(self, session_id: str) -> dict[str, Any]:
        connection = self.store._connection
        session = self.store.get(session_id)
        assert session is not None
        runs = self._rows("SELECT * FROM runs WHERE session_id=? ORDER BY started_at", session_id)
        run_ids = [item["id"] for item in runs]
        actions = self._rows("SELECT * FROM actions WHERE session_id=? ORDER BY created_at", session_id)
        action_ids = [item["id"] for item in actions]
        action_details: dict[str, dict[str, Any]] = {}
        for table in ("file_action_data", "command_action_data", "external_action_data"):
            if not action_ids:
                break
            marks = ",".join("?" for _ in action_ids)
            for row in connection.execute(f"SELECT * FROM {table} WHERE action_id IN ({marks})", action_ids):
                detail = dict(row)
                for key in ("argv", "payload", "result"):
                    if detail.get(key):
                        try:
                            detail[key] = json.loads(detail[key])
                        except json.JSONDecodeError:
                            pass
                action_details.setdefault(str(row["action_id"]), {}).update(detail)
        for action in actions:
            action["detail"] = action_details.get(action["id"], {})
        tool_calls: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        run_events: list[dict[str, Any]] = []
        if run_ids:
            marks = ",".join("?" for _ in run_ids)
            tool_calls = self._rows(f"SELECT * FROM tool_calls WHERE run_id IN ({marks}) ORDER BY id", *run_ids)
            citations = self._rows(f"SELECT * FROM citations WHERE run_id IN ({marks}) ORDER BY id", *run_ids)
            run_events = self._rows(f"SELECT * FROM run_events WHERE run_id IN ({marks}) ORDER BY run_id,sequence", *run_ids)
            for item in tool_calls:
                item["arguments"] = self._json_value(item["arguments"])
            for item in run_events:
                item["payload"] = self._json_value(item["payload"])
        document = {
            "format": SESSION_EXPORT_FORMAT,
            "version": SESSION_EXPORT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "session": {
                **asdict(session),
                "workspace": str(session.workspace),
                "title_source": session.title_source.value,
            },
            "messages": self._rows("SELECT * FROM messages WHERE session_id=? ORDER BY id", session_id),
            "runs": runs,
            "work_items": self._rows("SELECT * FROM work_items WHERE session_id=? ORDER BY created_at", session_id),
            "run_events": run_events,
            "tasks": self._rows("SELECT * FROM tasks WHERE session_id=? ORDER BY position,created_at", session_id),
            "sources": self._rows("SELECT * FROM sources WHERE session_id=? ORDER BY fetched_at", session_id),
            "actions": actions,
            "tool_calls": tool_calls,
            "citations": citations,
            "cost": dict(zip(("input_tokens", "output_tokens", "cost_usd"), self.store.session_cost(session_id))),
        }
        return redact(document)

    def _rows(self, query: str, *values: object) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store._connection.execute(query, values).fetchall()]

    @staticmethod
    def _json_value(value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    @staticmethod
    def _markdown(document: dict[str, Any]) -> str:
        session = document["session"]
        lines = [
            f"# {session['title']}",
            "",
            f"- Session: `{session['id']}`",
            f"- Workspace: `{session['workspace']}`",
            f"- Exported: {document['exported_at']}",
            "",
            "## Conversation",
            "",
        ]
        for message in document["messages"]:
            role = "User" if message["role"] == "user" else "CapsLock"
            lines.extend((f"### {role}", "", str(message["content"]), ""))
        lines.extend(("## Sources", ""))
        for source in document["sources"]:
            flag = " (suspicious)" if source["suspicious"] else ""
            lines.append(f"- [{source['title']}]({source['url']}){flag} - {source['fetched_at']}")
        lines.extend(("", "## Actions", ""))
        for action in document["actions"]:
            lines.append(
                f"- `{action['id'][:12]}` {action['action_type']} / {action['status']}: {action['summary']}"
            )
            detail = action.get("detail", {})
            if detail.get("diff"):
                lines.extend(("", "```diff", str(detail["diff"]), "```"))
            if detail.get("stdout") or detail.get("stderr"):
                lines.extend(("", "```text", str(detail.get("stdout", "")) + str(detail.get("stderr", "")), "```"))
        cost = document["cost"]
        lines.extend((
            "",
            "## Usage",
            "",
            f"Input tokens: {cost['input_tokens']}; output tokens: {cost['output_tokens']}; cost: ${cost['cost_usd']:.6f}",
            "",
        ))
        return "\n".join(lines)
