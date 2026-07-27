"""Application service for session plans and their controlled Markdown mirror."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from .domain import PlanRecord, PlanRevision
from .storage.repositories.plans import PlanRepository

MAX_PLAN_BYTES = 256 * 1024


class PlanningService:
    def __init__(self, repository: PlanRepository, *, root: Path) -> None:
        self.repository = repository
        self.root = root.resolve()

    async def create(
        self,
        session_id: str,
        objective: str,
        *,
        entry_source: str,
        base_permission_mode: str,
        parent_plan_id: str | None = None,
        content: str | None = None,
        revision_source: str = "initial",
    ) -> tuple[PlanRecord, PlanRevision]:
        normalized_objective = objective.strip() or "Plan the requested work"
        initial = content or f"# Plan\n\n## Goal\n\n{normalized_objective}\n"
        self.validate_content(initial)
        plan, revision = await self.repository.create(
            session_id,
            normalized_objective,
            entry_source=entry_source,
            base_permission_mode=base_permission_mode,
            content=initial,
            parent_plan_id=parent_plan_id,
            revision_source=revision_source,
        )
        await self.sync_mirror(plan, revision)
        return plan, revision

    async def current(self, session_id: str) -> tuple[PlanRecord, PlanRevision] | None:
        plan = await self.repository.active(session_id)
        if plan is None:
            return None
        revision = await self.repository.current_revision(plan)
        return plan, revision

    async def latest(self, session_id: str) -> tuple[PlanRecord, PlanRevision] | None:
        plan = await self.repository.latest(session_id)
        if plan is None:
            return None
        return plan, await self.repository.current_revision(plan)

    async def is_active(self, session_id: str) -> bool:
        return await self.repository.active(session_id) is not None

    async def attachment(self, session_id: str) -> str | None:
        current = await self.current(session_id)
        if current is None:
            return None
        plan, revision = current
        return (
            "<capslock-plan-mode>\n"
            "Plan Mode is active. You may only inspect local workspace state, ask "
            "the user questions, and read or update this session's plan through the "
            "dedicated plan tools. Do not invoke Shell, Web, MCP, plugins, child "
            "Agents, worktree, task mutation, memory mutation, or any Action-backed "
            "tool. Explore first, keep the plan implementation-ready, then call "
            "submit_plan with the exact current SHA-256.\n"
            f"Plan ID: {plan.id}\nObjective: {plan.objective}\n"
            f"Revision: {revision.ordinal}\nSHA-256: {revision.sha256}\n"
            "</capslock-plan-mode>"
        )

    async def clone_active(
        self,
        parent_session_id: str,
        child_session_id: str,
        *,
        entry_source: str,
        base_permission_mode: str,
    ) -> tuple[PlanRecord, PlanRevision] | None:
        current = await self.current(parent_session_id)
        if current is None:
            return None
        parent, revision = current
        return await self.create(
            child_session_id,
            parent.objective,
            entry_source=entry_source,
            base_permission_mode=base_permission_mode,
            parent_plan_id=parent.id,
            content=revision.content,
            revision_source="branch",
        )

    async def reconcile(self, session_id: str) -> None:
        current = await self.current(session_id)
        if current is not None:
            await self.sync_mirror(*current)
        for request in await self.repository.unreconciled_approved_requests(
            session_id
        ):
            await self.repository.ensure_implementation(request.id)

    async def update(
        self,
        session_id: str,
        content: str,
        *,
        expected_sha256: str,
        source: str,
        run_id: str | None = None,
    ) -> tuple[PlanRecord, PlanRevision]:
        self.validate_content(content)
        current = await self.current(session_id)
        if current is None:
            raise ValueError("plan mode is not active")
        plan, _ = current
        revision = await self.repository.append_revision(
            plan.id,
            content,
            expected_sha256=expected_sha256,
            source=source,
            run_id=run_id,
        )
        plan = await self.repository.require(plan.id)
        await self.sync_mirror(plan, revision)
        return plan, revision

    async def sync_mirror(self, plan: PlanRecord, revision: PlanRevision) -> Path:
        target = self.path(plan)
        await asyncio.to_thread(write_plan_mirror, target, revision.content)
        return target

    async def restore_mirror(self, session_id: str) -> Path:
        current = await self.current(session_id)
        if current is None:
            raise ValueError("plan mode is not active")
        return await self.sync_mirror(*current)

    def path(self, plan: PlanRecord) -> Path:
        return plan_mirror_path(self.root, plan.mirror_relative_path)

    @staticmethod
    def validate_content(content: str) -> None:
        if "\x00" in content:
            raise ValueError("plan must not contain NUL")
        if not content.strip():
            raise ValueError("plan must not be empty")
        if len(content.encode("utf-8")) > MAX_PLAN_BYTES:
            raise ValueError("plan exceeds the 256 KiB limit")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("plan mirror must not be a symbolic link")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def plan_mirror_path(root: Path, relative_path: str) -> Path:
    if root.is_symlink():
        raise ValueError("managed plan directory must not be a symbolic link")
    managed_root = root.resolve()
    candidate = managed_root / relative_path
    target = candidate.resolve(strict=False)
    if not target.is_relative_to(managed_root):
        raise ValueError("plan mirror escapes the managed plan directory")
    current = managed_root
    for part in Path(relative_path).parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise ValueError("plan mirror path must not contain symbolic links")
    return target


def write_plan_mirror(path: Path, content: str) -> None:
    PlanningService.validate_content(content)
    _atomic_write(path, content)


__all__ = [
    "MAX_PLAN_BYTES",
    "PlanningService",
    "plan_mirror_path",
    "write_plan_mirror",
]
