"""Approval-gated main-session Git worktree lifecycle."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Callable

from ...domain import ActionRecord, ActionResultKind, ActionType
from ...policy import WorkspacePolicy
from ...storage.repositories.core import now
from .core import ActionExecution, ActionProposal


class WorkspaceExecutionScope:
    def __init__(self, control_root: Path, policy: WorkspacePolicy) -> None:
        self.control_root = control_root.resolve()
        self.active_root = policy.root
        self.policy = policy
        self._listener: (
            Callable[[Path, WorkspacePolicy], Awaitable[None]] | None
        ) = None

    def bind(
        self, listener: Callable[[Path, WorkspacePolicy], Awaitable[None]]
    ) -> None:
        self._listener = listener

    async def switch(self, root: Path) -> None:
        resolved = root.resolve()
        policy = WorkspacePolicy(resolved)
        if self._listener is not None:
            await self._listener(resolved, policy)
        self.active_root, self.policy = resolved, policy


class WorktreeActionHandler:
    types = frozenset({ActionType.WORKTREE_CREATE, ActionType.WORKTREE_EXIT})

    def __init__(
        self,
        *,
        database: Any,
        session_id: str,
        state_root: Path,
        scope: WorkspaceExecutionScope,
        max_per_session: int = 4,
        process_manager: Any = None,
    ) -> None:
        self.database = database
        self.session_id = session_id
        self.state_root = state_root
        self.scope = scope
        self.max_per_session = max_per_session
        self.process_manager = process_manager

    async def propose(self, action_type: ActionType, payload: dict[str, Any]) -> ActionProposal:
        excluded = payload.get("_exclude_action")
        await self._preflight(
            exclude_action=str(excluded) if isinstance(excluded, str) else None
        )
        if action_type is ActionType.WORKTREE_CREATE:
            count = await self.database.fetch_one(
                "SELECT count(*) FROM session_worktrees WHERE session_id=? AND status!='removed'",
                (self.session_id,),
            )
            if int(count[0]) >= self.max_per_session:
                raise ValueError("session worktree limit reached")
            slug = _slug(str(payload.get("name") or "work"))
            identifier = f"wt_{uuid.uuid4().hex}"
            suffix = identifier[-8:]
            branch = f"capslock/{self.session_id[:8]}/{slug}-{suffix}"
            path = self.state_root / "worktrees" / self.session_id / f"{slug}-{suffix}"
            base = (await _git(self.scope.active_root, "rev-parse", "HEAD")).strip()
            request = {
                "worktree_id": identifier,
                "path": str(path),
                "branch": branch,
                "base_commit": base,
                "operation": "create",
            }
            _preserve_manual_approval(payload, request)
            return ActionProposal(
                f"Create and enter worktree {slug}",
                request,
            )
        row = await self.database.fetch_one(
            "SELECT * FROM session_worktrees WHERE session_id=? AND active=1",
            (self.session_id,),
        )
        if row is None:
            raise ValueError("this session has no active worktree")
        action = str(payload.get("action", "keep"))
        if action not in {"keep", "remove"}:
            raise ValueError("action must be keep or remove")
        discard = bool(payload.get("discard_changes", False))
        path = Path(str(row["path"]))
        dirty = bool((await _git(path, "status", "--porcelain")).strip())
        unmerged = bool((await _git(path, "log", "--oneline", f"{row['base_commit']}..HEAD")).strip())
        if action == "remove" and (dirty or unmerged) and not discard:
            raise ValueError("worktree has uncommitted or unmerged changes; set discard_changes=true")
        request = {
            "worktree_id": str(row["id"]),
            "path": str(path),
            "branch": str(row["branch"]),
            "base_commit": str(row["base_commit"]),
            "operation": action,
            "discard_changes": discard,
            "dirty": dirty,
            "unmerged": unmerged,
        }
        _preserve_manual_approval(payload, request)
        return ActionProposal(
            f"{action.capitalize()} active worktree",
            request,
        )

    async def execute(self, action: ActionRecord) -> ActionExecution:
        await self._preflight(exclude_action=action.id)
        request = action.request
        if action.type is ActionType.WORKTREE_CREATE:
            path = Path(str(request["path"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            await _git(self.scope.control_root, "worktree", "add", "-b", str(request["branch"]), str(path), str(request["base_commit"]))
            await self.database.execute("UPDATE session_worktrees SET active=0 WHERE session_id=?", (self.session_id,))
            await self.database.execute(
                "INSERT INTO session_worktrees(id,session_id,path,branch,base_commit,active,status,created_at) VALUES(?,?,?,?,?,1,'active',?)",
                (str(request["worktree_id"]), self.session_id, str(path), str(request["branch"]), str(request["base_commit"]), now()),
            )
            await self.scope.switch(path)
        else:
            path = Path(str(request["path"])).resolve()
            expected_root = (self.state_root / "worktrees" / self.session_id).resolve()
            if not path.is_relative_to(expected_root):
                raise ValueError("refusing to exit a worktree not owned by this session")
            await self.scope.switch(self.scope.control_root)
            operation = str(request["operation"])
            if operation == "remove":
                arguments = ["worktree", "remove"]
                if request.get("discard_changes"):
                    arguments.append("--force")
                arguments.append(str(path))
                await _git(self.scope.control_root, *arguments)
                await _git(self.scope.control_root, "branch", "-D", str(request["branch"]))
                status = "removed"
            else:
                status = "kept"
            await self.database.execute(
                "UPDATE session_worktrees SET active=0,status=?,exited_at=? WHERE id=? AND session_id=?",
                (status, now(), str(request["worktree_id"]), self.session_id),
            )
        return ActionExecution(
            {"active_workspace": str(self.scope.active_root), "operation": request["operation"]},
            ActionResultKind.SUCCESS,
        )

    async def _preflight(self, *, exclude_action: str | None = None) -> None:
        if self.process_manager is not None and self.process_manager.has_active(
            self.session_id
        ):
            raise ValueError("cannot switch worktrees while background Shell jobs run")
        action_values: list[object] = [self.session_id]
        action_query = (
            "SELECT 1 FROM actions WHERE session_id=? "
            "AND status IN ('pending','approved','running')"
        )
        if exclude_action is not None:
            action_query += " AND id!=?"
            action_values.append(exclude_action)
        action_query += " LIMIT 1"
        if await self.database.fetch_one(action_query, tuple(action_values)) is not None:
            raise ValueError("cannot switch worktrees while another Action is pending")
        active_agent = await self.database.fetch_one(
            """SELECT 1 FROM agent_tasks t JOIN runs r ON r.id=t.parent_run_id
               WHERE r.session_id=? AND t.state IN ('created','running','waiting_approval')
               LIMIT 1""",
            (self.session_id,),
        )
        if active_agent is not None:
            raise ValueError("cannot switch worktrees while child Agents are active")

    async def revalidate(self, action: ActionRecord) -> ActionProposal:
        if action.type is ActionType.WORKTREE_CREATE:
            await self._preflight(exclude_action=action.id)
            if Path(str(action.request["path"])).exists():
                raise ValueError("worktree path was created after proposal")
            return ActionProposal(action.summary, dict(action.request))
        return await self.propose(
            action.type,
            {
                "action": action.request["operation"],
                "discard_changes": action.request.get("discard_changes", False),
                "force_manual_approval": action.request.get(
                    "force_manual_approval", False
                ),
                "_exclude_action": action.id,
            },
        )

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("worktree actions are not automatically reversible")


async def _git(cwd: Path, *arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise ValueError(stderr.decode("utf-8", "replace").strip() or "Git worktree command failed")
    return stdout.decode("utf-8", "replace")


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (normalized or "work")[:40]


def _preserve_manual_approval(
    source: dict[str, Any], target: dict[str, Any]
) -> None:
    if source.get("force_manual_approval") is True:
        target["force_manual_approval"] = True
