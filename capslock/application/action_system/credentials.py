"""Approval-gated named credential delivery without secret persistence."""

from __future__ import annotations

import asyncio
from typing import Any

from ...credentials import resolve_credential
from ...domain import ActionRecord, ActionResultKind, ActionType
from .core import ActionExecution, ActionProposal


class CredentialActionHandler:
    types = frozenset({ActionType.CREDENTIAL_ACCESS})

    async def propose(
        self, action_type: ActionType, payload: dict[str, Any]
    ) -> ActionProposal:
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("credential name must be a non-empty string")
        return ActionProposal(
            f"Deliver named credential {name} to a plugin",
            {"name": name, "force_manual_approval": True},
        )

    async def execute(self, action: ActionRecord) -> ActionExecution:
        name = str(action.request["name"])
        secret = await asyncio.to_thread(_resolve_named_credential, name)
        if not secret:
            raise ValueError(f"credential is unavailable: {name}")
        return ActionExecution(
            {"name": name, "delivered": True}, ActionResultKind.SUCCESS
        )

    async def revalidate(self, action: ActionRecord) -> ActionProposal:
        return await self.propose(action.type, {"name": action.request.get("name")})

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("credential delivery cannot be reversed")


def resolve_named_credential(name: str) -> str:
    secret = _resolve_named_credential(name)
    if not secret:
        raise ValueError(f"credential is unavailable: {name}")
    return secret


def _resolve_named_credential(name: str) -> str | None:
    value = resolve_credential(f"env:{name}")
    return value if value else resolve_credential(f"keyring:{name}")
