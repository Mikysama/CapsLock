"""Immutable permission value objects shared by policy and middleware."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum

from ...permissions import PermissionMode

PERMISSIONS_VERSION = 2


class PermissionBehavior(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionDestination(StrEnum):
    SESSION = "session"
    LOCAL = "local"
    PROJECT = "project"
    USER = "user"


class PermissionUpdateOperation(StrEnum):
    ADD = "add"
    REMOVE = "remove"


@dataclass(frozen=True)
class PermissionUpdate:
    operation: PermissionUpdateOperation
    destination: PermissionDestination
    behavior: PermissionBehavior
    tool: str
    constraints: dict[str, object] = field(default_factory=dict)
    rule_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "destination": self.destination.value,
            "behavior": self.behavior.value,
            "tool": self.tool,
            "constraints": json.loads(json.dumps(self.constraints)),
            "rule_id": self.rule_id,
        }


@dataclass(frozen=True)
class PermissionRule:
    behavior: PermissionBehavior
    tool: str
    constraints: dict[str, object]
    source: str
    identifier: str | None = None
    matcher_version: int = PERMISSIONS_VERSION
    diagnostic: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.identifier,
            "behavior": self.behavior.value,
            "tool": self.tool,
            "constraints": json.loads(json.dumps(self.constraints)),
            "source": self.source,
            "matcher_version": self.matcher_version,
            "diagnostic": self.diagnostic,
        }

    @property
    def specificity(self) -> tuple[int, int, int]:
        exact = sum(
            1
            for key, value in self.constraints.items()
            if key not in {"path", "cwd", "command_prefix", "host"}
            or isinstance(value, str)
            and not any(char in value for char in "*?[")
        )
        literal = sum(
            len(str(value).replace("*", "")) for value in self.constraints.values()
        )
        return exact, len(self.constraints), literal


@dataclass(frozen=True)
class PermissionDecision:
    behavior: PermissionBehavior
    source: str
    reason: str
    reason_code: str
    mode: PermissionMode
    rule: PermissionRule | None = None
    normalized_arguments_sha256: str = ""
    input_modified: bool = False
    suggestions: tuple[PermissionUpdate, ...] = ()
    decided_by: str | None = None
    classifier: dict[str, object] | None = None

    @property
    def persistence_suggestion(self) -> str | None:
        return self.suggestions[0].destination.value if self.suggestions else None

    def as_dict(self, *, include_rule: bool = True) -> dict[str, object]:
        return {
            "behavior": self.behavior.value,
            "source": self.source,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "mode": self.mode.value,
            "normalized_arguments_sha256": self.normalized_arguments_sha256,
            "input_modified": self.input_modified,
            "suggestions": [item.as_dict() for item in self.suggestions],
            "decided_by": self.decided_by,
            "classifier": self.classifier,
            **(
                {"rule": self.rule.as_dict() if self.rule else None}
                if include_rule
                else {}
            ),
        }


__all__ = [
    "PERMISSIONS_VERSION",
    "PermissionBehavior",
    "PermissionDecision",
    "PermissionDestination",
    "PermissionRule",
    "PermissionUpdate",
    "PermissionUpdateOperation",
]
