"""Approval-gated file action handler."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from typing import Any

from ...changes import make_diff
from ...domain import ActionRecord, ActionResultKind, ActionType
from ...policy import WorkspacePolicy
from .core import ActionExecution, ActionProposal


def content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


class FileActionHandler:
    types = frozenset({ActionType.FILE_EDIT, ActionType.FILE_CREATE})

    def __init__(self, policy: WorkspacePolicy) -> None:
        self.policy = policy

    async def propose(
        self, action_type: ActionType, payload: dict[str, Any]
    ) -> ActionProposal:
        if action_type is ActionType.FILE_EDIT:
            return await asyncio.to_thread(self._propose_edit, payload)
        return await asyncio.to_thread(self._propose_create, payload)

    def _propose_edit(self, payload: dict[str, Any]) -> ActionProposal:
        path_text = _string(payload, "path")
        old_text, new_text = _string(payload, "old_text"), _string(payload, "new_text")
        if not old_text:
            raise ValueError(
                "old_text must be non-empty; use file create for new files"
            )
        path = self.policy.writable_file(path_text)
        before = path.read_text(encoding="utf-8")
        if before.count(old_text) != 1:
            raise ValueError("old_text must occur exactly once in the current file")
        after = before.replace(old_text, new_text, 1)
        self.policy.validate_write_content(after)
        relative = str(path.relative_to(self.policy.root))
        return ActionProposal(
            str(payload.get("summary") or "Edit file"),
            {
                "path": relative,
                "operation": "edit",
                "expected_hash": content_hash(before),
                "before_content": before,
                "after_content": after,
                "diff": make_diff(Path(relative), before, after),
            },
        )

    def _propose_create(self, payload: dict[str, Any]) -> ActionProposal:
        path_text, content = _string(payload, "path"), _string(payload, "content")
        path = self.policy.writable_file(path_text, create=True)
        if path.exists():
            raise ValueError("file already exists; use file edit")
        self.policy.validate_write_content(content)
        relative = str(path.relative_to(self.policy.root))
        return ActionProposal(
            str(payload.get("summary") or "Create file"),
            {
                "path": relative,
                "operation": "create",
                "expected_hash": None,
                "before_content": None,
                "after_content": content,
                "diff": make_diff(Path(relative), None, content),
            },
        )

    async def execute(self, action: ActionRecord) -> ActionExecution:
        await asyncio.to_thread(self._apply, action)
        return ActionExecution(
            {"path": action.request["path"], "operation": action.request["operation"]},
            ActionResultKind.APPLIED,
        )

    def _apply(self, action: ActionRecord) -> None:
        request = action.request
        path = self.policy.writable_file(
            str(request["path"]), create=request["operation"] == "create"
        )
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if (
            request["operation"] == "edit"
            and content_hash(current or "") != request["expected_hash"]
        ):
            raise ValueError("file changed after proposal; create a new proposal")
        if request["operation"] == "create" and path.exists():
            raise ValueError("file was created after proposal; create a new proposal")
        content = str(request["after_content"])
        self.policy.validate_write_content(content)
        path.write_text(content, encoding="utf-8")

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        return await asyncio.to_thread(self._reverse, action)

    def _reverse(self, action: ActionRecord) -> dict[str, Any]:
        request = action.request
        path = self.policy.writable_file(
            str(request["path"]), create=request["operation"] == "create"
        )
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != request["after_content"]:
            raise ValueError("file changed after application; refusing unsafe undo")
        if request["operation"] == "create":
            path.unlink()
        else:
            path.write_text(str(request["before_content"]), encoding="utf-8")
        return {"path": str(request["path"]), "reversed": True}


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value
