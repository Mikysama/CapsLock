"""Ports used by permission policy without coupling it to persistence adapters."""

from __future__ import annotations

from typing import Any, Protocol

from .models import PermissionBehavior, PermissionRule, PermissionUpdate
from ..contracts import ExecutionContext, ResolvedToolPolicy, ToolDefinition


class PermissionSpec(Protocol):
    def normalize(
        self, tool: ToolDefinition, arguments: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]: ...

    def hard_check(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        context: ExecutionContext,
    ) -> tuple[PermissionBehavior, str, str] | None: ...

    def matches(
        self,
        rule: PermissionRule,
        tool: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool: ...

    def suggest_updates(
        self, tool: ToolDefinition, arguments: dict[str, Any]
    ) -> tuple[PermissionUpdate, ...]: ...


class PermissionStorePort(Protocol):
    async def session_permission_rules(
        self, session_id: str
    ) -> list[dict[str, Any]]: ...
    async def add_session_permission_rule(
        self, session_id: str, **values: Any
    ) -> str: ...
    async def remove_session_permission_rule(
        self, session_id: str, rule_id: str
    ) -> None: ...
    async def permission_setting(self, key: str) -> str | None: ...
    async def set_permission_setting(self, key: str, value: str) -> None: ...


__all__ = ["PermissionSpec", "PermissionStorePort"]
