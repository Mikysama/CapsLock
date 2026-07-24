"""Audit-only handler for an approved multi-file session rewind."""

from __future__ import annotations

from ...domain import ActionRecord, ActionResultKind, ActionType
from .core import ActionExecution, ActionProposal


class SessionRewindActionHandler:
    types = frozenset({ActionType.SESSION_REWIND})

    async def propose(self, action_type: ActionType, payload: dict) -> ActionProposal:
        request = dict(payload)
        request["force_manual_approval"] = True
        return ActionProposal(
            str(payload.get("summary") or "Rewind session files"), request
        )

    async def execute(self, action: ActionRecord) -> ActionExecution:
        return ActionExecution(dict(action.request), ActionResultKind.APPLIED)

    async def revalidate(self, action: ActionRecord) -> ActionProposal:
        return await self.propose(action.type, action.request)

    async def reverse(self, action: ActionRecord) -> dict:
        raise ValueError("a session rewind cannot be automatically reversed")
