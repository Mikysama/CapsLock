"""Slash-command handlers for worktree operations."""

from __future__ import annotations

import json

from ...domain import (
    ActionStatus,
    ActionType,
    ApprovalDecision,
    RunKind,
)
from ..commands import CommandOutcome
from .support import get_repositories, get_ui


async def worktree(context, parts: list[str], raw: str) -> CommandOutcome:
    repositories = get_repositories(context)
    operation = parts[1] if len(parts) > 1 else "list"
    if operation == "list":
        rows = await repositories.database.fetch_all(
            "SELECT id,branch,path,status,active FROM session_worktrees WHERE session_id=? ORDER BY created_at",
            (context.session.session_id,),
        )
        content = "\n".join(
            f"{r['id'][:12]} {r['status']}{' active' if r['active'] else ''} {r['branch']} {r['path']}"
            for r in rows
        )
        await get_ui(context).show("Session worktrees", content or "No worktrees.")
        return CommandOutcome()
    if operation == "create" and len(parts) == 3:
        action_type, payload = (
            ActionType.WORKTREE_CREATE,
            {"name": parts[2], "force_manual_approval": True},
        )
    elif operation == "exit":
        mode = next((item for item in parts[2:] if item in {"keep", "remove"}), "keep")
        unknown = set(parts[2:]) - {"keep", "remove", "--discard"}
        if unknown:
            raise ValueError("usage: /worktree exit [keep|remove] [--discard]")
        action_type, payload = (
            ActionType.WORKTREE_EXIT,
            {
                "action": mode,
                "discard_changes": "--discard" in parts,
                "force_manual_approval": True,
            },
        )
    else:
        raise ValueError(
            "usage: /worktree [list|create <name>|exit [keep|remove] [--discard]]"
        )
    audit = await repositories.runs.create_hidden(
        context.session.session_id, kind=RunKind.LOCAL_COMMAND
    )
    coordinator = context.session.action_factory(audit.id)
    previous = coordinator.approval_authorizer
    coordinator.approval_authorizer = lambda action: _approve_action(
        get_ui(context), action
    )
    try:
        action = await coordinator.propose(action_type, **payload)
        status = "completed" if action.status is ActionStatus.COMPLETED else "cancelled"
        await repositories.runs.finish_hidden(audit.id, status=status)
        context.console.print(
            f"[{'success' if status == 'completed' else 'warning'}]{action.status.value}:[/] {action.summary}"
        )
    except BaseException as exc:
        await repositories.runs.finish_hidden(
            audit.id, status="failed", error_message=str(exc)
        )
        raise
    finally:
        coordinator.approval_authorizer = previous
    return CommandOutcome()


async def _approve_action(ui, action) -> ApprovalDecision:
    approved = await ui.confirm(
        action.summary,
        json.dumps(action.request, ensure_ascii=False, indent=2),
        default=False,
    )
    return ApprovalDecision.APPROVE if approved else ApprovalDecision.REJECT
