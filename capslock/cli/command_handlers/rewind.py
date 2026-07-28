"""Slash-command handlers for rewind operations."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from ...domain import (
    RunKind,
)
from ...storage.repositories.core import now
from ..command_ui import Choice
from ..commands import CommandOutcome, CommandOutcomeKind
from .support import get_repositories, get_ui


async def rewind(context, parts: list[str], raw: str) -> CommandOutcome:
    if len(parts) > 2:
        raise ValueError("usage: /rewind [run-id-prefix]")
    repositories = get_repositories(context)
    session_id = context.session.session_id
    rows = await repositories.database.fetch_all(
        """SELECT r.* FROM runs r WHERE r.session_id=? AND r.kind='agent' AND r.status='completed'
           AND EXISTS(SELECT 1 FROM messages m WHERE m.run_id=r.id AND m.role='assistant' AND length(m.content)>0)
           ORDER BY r.started_at DESC""",
        (session_id,),
    )
    if len(parts) == 2:
        matches = [row for row in rows if str(row["id"]).startswith(parts[1])]
        if len(matches) != 1:
            raise ValueError(
                "run prefix is ambiguous or does not identify a completed agent run"
            )
        target = matches[0]
    else:
        selected = await get_ui(context).select(
            "Rewind to run",
            [Choice(str(r["id"]), str(r["question"]), str(r["id"])[:8]) for r in rows],
        )
        if not selected:
            return CommandOutcome()
        target = next(row for row in rows if row["id"] == selected)
    later = await repositories.database.fetch_all(
        """SELECT a.* FROM actions a JOIN runs r ON r.id=a.run_id WHERE a.session_id=?
           AND r.started_at>? AND a.status='completed' AND a.reversed_at IS NULL ORDER BY a.created_at DESC""",
        (session_id, target["started_at"]),
    )
    blockers: list[str] = []
    file_rows = []
    for row in later:
        kind = str(row["action_type"])
        if kind in {"file_edit", "file_create", "notebook_edit"}:
            file_rows.append(row)
        elif kind in {
            "command",
            "mcp_connect",
            "mcp_call",
            "credential_access",
            "worktree_create",
            "worktree_exit",
        }:
            blockers.append(f"{kind} action {str(row['id'])[:8]} is not reversible")
    active_worktree = await repositories.database.fetch_one(
        "SELECT 1 FROM session_worktrees WHERE session_id=? AND active=1", (session_id,)
    )
    if active_worktree:
        blockers.append("the session has an active worktree")
    simulated: dict[str, str | None] = {}
    requests: list[tuple[object, dict]] = []
    for row in file_rows:
        request = json.loads(row["request_json"])
        path = context.session.policy.writable_file(str(request["path"]), create=True)
        current = simulated.get(
            str(path), path.read_text(encoding="utf-8") if path.exists() else None
        )
        if current != request.get("after_content"):
            blockers.append(f"file hash/content conflict: {request['path']}")
        else:
            simulated[str(path)] = request.get("before_content")
        requests.append((row, request))
    parent = await repositories.sessions.require(session_id)
    child_title = f"{parent.title} (Rewind)"
    if blockers:
        approved = await get_ui(context).confirm(
            "File restore blocked",
            "\n".join(blockers) + "\n\nCreate a conversation-only rewind branch?",
            default=False,
        )
        if not approved:
            return CommandOutcome()
        child = await repositories.sessions.derive(
            session_id,
            title=child_title,
            derivation_kind="rewind",
            target_run_id=str(target["id"]),
        )
        if context.session.planning is not None:
            await context.session.planning.clone_active(
                session_id,
                child.id,
                entry_source="rewind",
                base_permission_mode=context.session.permission_mode.value,
            )
        return CommandOutcome(CommandOutcomeKind.SWITCH_SESSION, child.id)
    detail = (
        "\n\n".join(
            str(request.get("diff") or request["path"]) for _, request in requests
        )
        or "No files need restoration."
    )
    if not await get_ui(context).confirm(
        "Restore files and create rewind branch", detail, default=False
    ):
        return CommandOutcome()
    originals: dict[Path, str | None] = {}
    audit = await repositories.runs.create_hidden(
        session_id, kind=RunKind.LOCAL_COMMAND
    )
    child = await repositories.sessions.derive(
        session_id,
        title=child_title,
        derivation_kind="rewind",
        target_run_id=str(target["id"]),
    )
    if context.session.planning is not None:
        await context.session.planning.clone_active(
            session_id,
            child.id,
            entry_source="rewind",
            base_permission_mode=context.session.permission_mode.value,
        )
    try:
        for _, request in requests:
            path = context.session.policy.writable_file(
                str(request["path"]), create=True
            )
            originals.setdefault(
                path, path.read_text(encoding="utf-8") if path.exists() else None
            )
            before = request.get("before_content")
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(str(before), encoding="utf-8")
        timestamp, action_id = now(), f"rewind_{uuid.uuid4().hex}"
        async with repositories.database.transaction() as connection:
            if file_rows:
                await connection.executemany(
                    "UPDATE actions SET reversed_at=? WHERE id=? AND reversed_at IS NULL",
                    [(timestamp, row["id"]) for row in file_rows],
                )
            await connection.execute(
                """INSERT INTO actions(id,session_id,run_id,action_type,status,result_kind,summary,request_json,result_json,
                   risk_level,risk_reason,rollback,created_at,approved_at,started_at,finished_at,decided_at)
                   VALUES(?,?,?,'session_rewind','completed','applied',?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    action_id,
                    session_id,
                    audit.id,
                    "Restore files for session rewind",
                    json.dumps(
                        {
                            "target_run_id": target["id"],
                            "files": [r[1]["path"] for r in requests],
                        }
                    ),
                    json.dumps({"restored": True}),
                    "high",
                    "Restores prior file contents.",
                    "Compensated atomically on failure.",
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        await repositories.runs.finish_hidden(audit.id)
    except BaseException as exc:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(content, encoding="utf-8")
        await repositories.runs.finish_hidden(
            audit.id, status="failed", error_message=str(exc)
        )
        await repositories.sessions.delete(child.id)
        raise
    return CommandOutcome(CommandOutcomeKind.SWITCH_SESSION, child.id)
