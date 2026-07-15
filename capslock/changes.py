"""Proposal, approval, application, and reversal of controlled text changes."""

from __future__ import annotations

import difflib
from hashlib import sha256
from pathlib import Path

from .policy import PolicyError, WorkspacePolicy
from .session import ChangeInfo, SessionStore


def content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def make_diff(path: Path, before: str | None, after: str) -> str:
    before_lines = [] if before is None else before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    return "".join(difflib.unified_diff(before_lines, after_lines, fromfile=f"a/{path}", tofile=f"b/{path}"))


class ChangeService:
    def __init__(self, store: SessionStore, policy: WorkspacePolicy, session_id: str, run_id: str, emit) -> None:
        self.store, self.policy, self.session_id, self.run_id, self.emit = store, policy, session_id, run_id, emit

    def propose_edit(self, path_text: str, old_text: str, new_text: str, summary: str) -> ChangeInfo:
        if not old_text:
            raise ValueError("old_text must be non-empty; use propose_file_create for new files")
        path = self.policy.writable_file(path_text)
        before = path.read_text(encoding="utf-8")
        if before.count(old_text) != 1:
            raise ValueError("old_text must occur exactly once in the current file")
        after = before.replace(old_text, new_text, 1)
        self.policy.validate_write_content(after)
        change = self.store.create_change(
            session_id=self.session_id, run_id=self.run_id, path=str(path.relative_to(self.policy.root)), operation="edit",
            expected_hash=content_hash(before), before_content=before, after_content=after,
            diff=make_diff(path.relative_to(self.policy.root), before, after), summary=summary or "Edit file",
        )
        self.emit("change_proposed", change_id=change.id, path=change.path, operation=change.operation)
        return change

    def propose_create(self, path_text: str, content: str, summary: str) -> ChangeInfo:
        path = self.policy.writable_file(path_text, create=True)
        if path.exists():
            raise ValueError("file already exists; use propose_file_edit")
        self.policy.validate_write_content(content)
        change = self.store.create_change(
            session_id=self.session_id, run_id=self.run_id, path=str(path.relative_to(self.policy.root)), operation="create",
            expected_hash=None, before_content=None, after_content=content,
            diff=make_diff(path.relative_to(self.policy.root), None, content), summary=summary or "Create file",
        )
        self.emit("change_proposed", change_id=change.id, path=change.path, operation=change.operation)
        return change

    def approve(self, change_id: str) -> ChangeInfo:
        change = self._change(change_id)
        if change.status != "pending":
            raise ValueError(f"change is not pending: {change.status}")
        self.store.update_change_status(change.id, "approved")
        self.emit("change_approved", change_id=change.id)
        return self._change(change.id)

    def reject(self, change_id: str) -> ChangeInfo:
        change = self._change(change_id)
        if change.status != "pending":
            raise ValueError(f"change is not pending: {change.status}")
        self.store.update_change_status(change.id, "discarded")
        self.emit("change_discarded", change_id=change.id)
        return self._change(change.id)

    def apply(self, change_id: str) -> ChangeInfo:
        change = self._change(change_id)
        if change.status != "approved":
            raise ValueError("change requires explicit approval before it can be applied")
        path = self.policy.writable_file(change.path, create=change.operation == "create")
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if change.operation == "edit" and content_hash(current or "") != change.expected_hash:
            self.store.update_change_status(change.id, "failed", error="file changed after proposal")
            raise ValueError("file changed after proposal; create a new proposal")
        if change.operation == "create" and path.exists():
            self.store.update_change_status(change.id, "failed", error="file was created after proposal")
            raise ValueError("file was created after proposal; create a new proposal")
        self.policy.validate_write_content(change.after_content)
        self.store.update_change_status(change.id, "running")
        path.write_text(change.after_content, encoding="utf-8")
        self.store.update_change_status(change.id, "applied")
        self.emit("change_applied", change_id=change.id, path=change.path)
        return self._change(change.id)

    def undo_last(self) -> ChangeInfo:
        change = self.store.last_applied_change(self.session_id)
        if change is None:
            raise ValueError("no applied change is available to undo")
        path = self.policy.writable_file(change.path, create=change.operation == "create")
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != change.after_content:
            raise ValueError("file changed after application; refusing unsafe undo")
        if change.operation == "create":
            # v1.1 does not otherwise expose deletion; this only reverses an agent-created file.
            path.unlink()
        else:
            assert change.before_content is not None
            path.write_text(change.before_content, encoding="utf-8")
        self.store.update_change_status(change.id, "undone")
        self.emit("change_undone", change_id=change.id, path=change.path)
        return self._change(change.id)

    def _change(self, change_id: str) -> ChangeInfo:
        change = self.store.get_change(change_id, session_id=self.session_id)
        if change is None:
            raise PolicyError("change does not belong to this session or does not exist")
        return change
