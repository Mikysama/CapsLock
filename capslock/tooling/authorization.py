"""Structured, explainable tool permission rules and runtime middleware."""

from __future__ import annotations

import fnmatch
import json
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from ..permissions import PermissionMode
from .contracts import (
    ExecutionContext,
    ResolvedToolPolicy,
    ToolDefinition,
    ToolMiddleware,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
)


class PermissionBehavior(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionRule:
    behavior: PermissionBehavior
    tool: str
    constraints: dict[str, object]
    source: str
    identifier: str | None = None

    def matches(self, tool: str, arguments: dict[str, Any]) -> bool:
        if not fnmatch.fnmatchcase(tool, self.tool):
            return False
        for key, expected in self.constraints.items():
            actual = arguments.get(key)
            if key in {"path", "cwd", "command", "server", "tool", "sandbox"}:
                patterns = expected if isinstance(expected, list) else [expected]
                if not isinstance(actual, str) or not any(
                    fnmatch.fnmatchcase(actual, str(pattern)) for pattern in patterns
                ):
                    return False
            elif key == "network":
                requested = arguments.get("network", [])
                requested_values = (
                    [requested]
                    if isinstance(requested, str)
                    else list(requested)
                    if isinstance(requested, list)
                    else []
                )
                allowed = expected if isinstance(expected, list) else [expected]
                if not all(
                    any(
                        fnmatch.fnmatchcase(str(item), str(pattern))
                        for pattern in allowed
                    )
                    for item in requested_values
                ):
                    return False
            elif actual != expected:
                return False
        return True

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.identifier,
            "behavior": self.behavior.value,
            "tool": self.tool,
            "constraints": self.constraints,
            "source": self.source,
        }


@dataclass(frozen=True)
class PermissionDecision:
    behavior: PermissionBehavior
    source: str
    reason: str
    rule: PermissionRule | None = None
    input_modified: bool = False
    persistence_suggestion: str | None = None
    decided_by: str | None = None
    classifier: dict[str, object] | None = None


class PermissionEngine:
    """Merge hard boundaries, three TOML sources and database session rules."""

    def __init__(self, paths: Iterable[tuple[str, Path]], repository: Any) -> None:
        self.paths = tuple(paths)
        self.repository = repository

    async def decide(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        context: ExecutionContext,
    ) -> PermissionDecision:
        hard_reason = self._hard_deny(tool.name, arguments, policy, context)
        if hard_reason:
            return PermissionDecision(PermissionBehavior.DENY, "hard_deny", hard_reason)
        rules = list(self._file_rules())
        if hasattr(self.repository, "session_permission_rules"):
            for raw in await self.repository.session_permission_rules(
                context.session_id
            ):
                rules.append(_parse_rule(raw, "session"))
        matching = [rule for rule in rules if rule.matches(tool.name, arguments)]
        for behavior in (
            PermissionBehavior.DENY,
            PermissionBehavior.ASK,
            PermissionBehavior.ALLOW,
        ):
            selected = next(
                (item for item in matching if item.behavior is behavior), None
            )
            if selected is not None:
                return PermissionDecision(
                    behavior,
                    selected.source,
                    f"matched {selected.source} permission rule for {selected.tool}",
                    selected,
                    persistence_suggestion="session"
                    if behavior is PermissionBehavior.ASK
                    else None,
                )
        classifier = context.runtime_state.get("shell_classifier")
        if (
            tool.name == "shell"
            and context.runtime_state.get("classifier_auto_allow") is True
            and isinstance(classifier, dict)
        ):
            return PermissionDecision(
                PermissionBehavior.ALLOW,
                "shell_classifier",
                "fast classifier allowed the command inside deterministic sandbox boundaries",
                decided_by="routing.fast",
                classifier=classifier,
            )
        if context.permission_mode is PermissionMode.FULL_ACCESS:
            default = PermissionBehavior.ALLOW
        elif context.permission_mode is PermissionMode.ASK_FOR_APPROVAL:
            default = PermissionBehavior.ASK
        elif policy.destructive or policy.external_side_effects or policy.open_world:
            default = PermissionBehavior.ASK
        else:
            default = PermissionBehavior.ALLOW
        return PermissionDecision(
            default,
            "permission_mode",
            f"defaulted from permission mode {context.permission_mode.value}",
        )

    def _file_rules(self) -> Iterable[PermissionRule]:
        for source, path in self.paths:
            if not path.is_file():
                continue
            try:
                document = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise ValueError(f"invalid permissions file {path}: {exc}") from exc
            raw_rules = document.get("rules", document.get("rule", []))
            if not isinstance(raw_rules, list):
                raise ValueError(f"permissions rules must be an array in {path}")
            for raw in raw_rules:
                if not isinstance(raw, dict):
                    raise ValueError(f"permission rule must be a table in {path}")
                yield _parse_rule(raw, source)

    @staticmethod
    def _hard_deny(
        name: str,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        context: ExecutionContext,
    ) -> str | None:
        del policy
        for key in ("path", "cwd"):
            value = arguments.get(key)
            if isinstance(value, str):
                try:
                    context.policy.resolve(value)
                except Exception:
                    return f"{key} escapes the workspace boundary"
        if name == "shell" and arguments.get("sandbox") == "host":
            return "host shell execution is outside the automatic permission boundary"
        return None


class PermissionMiddleware(ToolMiddleware):
    def __init__(self, engine: PermissionEngine) -> None:
        self.engine = engine

    async def normalize(
        self, tool: ToolDefinition, arguments: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]:
        del tool, context
        return arguments

    async def authorize(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        context: ExecutionContext,
    ) -> ToolOutcome | None:
        decision = await self.engine.decide(tool, arguments, policy, context)
        context.event(
            "tool_permission",
            name=tool.name,
            behavior=decision.behavior.value,
            source=decision.source,
            reason=decision.reason,
        )
        permission_emit = context.runtime_state.get("permission_emit")
        if callable(permission_emit):
            await permission_emit(
                {
                    "behavior": decision.behavior.value,
                    "source": decision.source,
                    "reason": decision.reason,
                    "input_modified": decision.input_modified,
                    "persistence_suggestion": decision.persistence_suggestion,
                    "decided_by": decision.decided_by,
                }
            )
        if context.invocation_id and hasattr(
            self.engine.repository, "record_permission_decision"
        ):
            await self.engine.repository.record_permission_decision(
                invocation_id=context.invocation_id,
                behavior=decision.behavior.value,
                source=decision.source,
                reason=decision.reason,
                rule=decision.rule.as_dict() if decision.rule else None,
                classifier=decision.classifier,
                decided_by=decision.decided_by,
            )
        if decision.behavior is PermissionBehavior.DENY:
            return ToolOutcome(
                ToolOutcomeStatus.DENIED,
                False,
                error=decision.reason,
                error_code="permission_denied",
            )
        if decision.behavior is PermissionBehavior.ASK:
            # Durable Action-backed tools pause and revalidate in ActionCoordinator.
            if tool.name in {
                "edit_file",
                "create_file",
                "shell",
                "run_check",
                "web_search",
                "web_fetch",
                "write_file",
                "edit_notebook",
                "create_worktree",
                "exit_worktree",
            } or tool.name.startswith(("plugin__", "mcp__")):
                context.runtime_state["force_manual_approval"] = True
                return None
            return ToolPause(
                "approval",
                f"permission:{context.invocation_id or tool.name}",
                {
                    "tool": tool.name,
                    "reason": decision.reason,
                    "arguments": arguments,
                },
                {"permission_source": decision.source},
            )
        return None

    async def after(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        outcome: ToolOutcome,
        context: ExecutionContext,
    ) -> ToolOutcome:
        del tool, arguments, policy, context
        return outcome


def _parse_rule(raw: dict[str, Any], source: str) -> PermissionRule:
    try:
        behavior = PermissionBehavior(str(raw["behavior"]))
        tool = str(raw["tool"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid {source} permission rule") from exc
    constraints = raw.get("constraints", raw.get("constraint", {}))
    if not isinstance(constraints, dict):
        raise ValueError(f"permission constraint must be a table in {source}")
    return PermissionRule(
        behavior,
        tool,
        json.loads(json.dumps(constraints)),
        source,
        str(raw["id"]) if raw.get("id") is not None else None,
    )


__all__ = [
    "PermissionBehavior",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionMiddleware",
    "PermissionRule",
]
