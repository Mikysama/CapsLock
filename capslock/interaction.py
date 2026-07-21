"""Mutable per-workspace user interaction state shared by runtime services."""

from __future__ import annotations

from dataclasses import dataclass

from .permissions import PermissionMode
from .ports import ActionAuthorizer


@dataclass
class RunInteraction:
    permission_mode: PermissionMode = PermissionMode.APPROVE_FOR_ME
    action_authorizer: ActionAuthorizer | None = None
