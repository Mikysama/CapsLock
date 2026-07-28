"""Pure permission rule selection and mode fallback policy."""

from __future__ import annotations

from collections.abc import Iterable

from ...permissions import PermissionMode
from .models import PermissionBehavior, PermissionRule
from ..contracts import ResolvedToolPolicy

_SOURCE_PRIORITY = {"user": 0, "project": 1, "local": 2, "session": 3}


class PermissionDecisionEngine:
    """Evaluate already-normalized inputs without persistence or filesystem I/O."""

    def select_rule(
        self,
        rules: Iterable[PermissionRule],
    ) -> PermissionRule | None:
        matching = tuple(rules)
        for behavior in (
            PermissionBehavior.DENY,
            PermissionBehavior.ASK,
            PermissionBehavior.ALLOW,
        ):
            candidates = [item for item in matching if item.behavior is behavior]
            if candidates:
                return max(
                    candidates,
                    key=lambda item: (
                        item.specificity,
                        _SOURCE_PRIORITY.get(item.source, -1),
                        item.identifier or "",
                    ),
                )
        return None

    def default_behavior(
        self,
        *,
        mode: PermissionMode,
        policy: ResolvedToolPolicy,
        deterministic_shell_allow: bool = False,
        shell_tool: bool = False,
    ) -> PermissionBehavior:
        if mode is PermissionMode.APPROVE_FOR_ME and shell_tool:
            return (
                PermissionBehavior.ALLOW
                if deterministic_shell_allow
                else PermissionBehavior.ASK
            )
        if mode is PermissionMode.FULL_ACCESS:
            return PermissionBehavior.ALLOW
        if mode is PermissionMode.ASK_FOR_APPROVAL:
            return PermissionBehavior.ASK
        if policy.destructive or policy.external_side_effects or policy.open_world:
            return PermissionBehavior.ASK
        return PermissionBehavior.ALLOW


__all__ = ["PermissionDecisionEngine"]
