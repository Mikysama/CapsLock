"""Permission middleware adapter for the Tool Runtime."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from .models import PermissionBehavior, PermissionRule, PermissionUpdate
from ..contracts import ToolMiddleware, ToolOutcome, ToolOutcomeStatus, ToolPause

_ACTION_TOOLS = {
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
}


class PermissionMiddleware(ToolMiddleware):
    def __init__(self, engine: Any) -> None:
        self.engine = engine

    async def normalize(self, tool, arguments, context):
        return self.engine.normalize(tool, arguments, context)

    async def authorize(self, tool, arguments, policy, context):
        if context.runtime_state.pop("planning_control_authorized", False):
            return None
        decision = await self.engine.decide(tool, arguments, policy, context)
        classifier = context.runtime_state.get("shell_classifier")
        if decision.classifier is None and isinstance(classifier, dict):
            decision = replace(decision, classifier=classifier)
        context.runtime_state["permission_decision"] = decision.as_dict()
        context.event(
            "tool_permission",
            name=tool.name,
            behavior=decision.behavior.value,
            source=decision.source,
            reason=decision.reason,
            reason_code=decision.reason_code,
        )
        permission_emit = context.runtime_state.get("permission_emit")
        if callable(permission_emit):
            await permission_emit(decision.as_dict(include_rule=False))
        if context.invocation_id and hasattr(
            self.engine.repository, "record_permission_decision"
        ):
            await self.engine.repository.record_permission_decision(
                invocation_id=context.invocation_id,
                behavior=decision.behavior.value,
                source=decision.source,
                reason=decision.reason,
                reason_code=decision.reason_code,
                mode=decision.mode.value,
                arguments_sha256=decision.normalized_arguments_sha256,
                rule=_audit_rule(decision.rule) if decision.rule else None,
                classifier=decision.classifier,
                decided_by=decision.decided_by,
                suggestions=_audit_suggestions(decision.suggestions),
            )
        if decision.behavior is PermissionBehavior.DENY:
            return ToolOutcome(
                ToolOutcomeStatus.DENIED,
                False,
                error=decision.reason,
                error_code="permission_denied",
            )
        if decision.behavior is PermissionBehavior.ASK:
            if tool.name in _ACTION_TOOLS or tool.name.startswith(
                ("plugin__", "mcp__")
            ):
                context.runtime_state["force_manual_approval"] = True
                return None
            if (
                hasattr(self.engine.repository, "create_permission_request")
                and context.invocation_id
            ):
                request_id = await self.engine.repository.create_permission_request(
                    session_id=context.session_id,
                    run_id=context.run_id,
                    invocation_id=context.invocation_id,
                    tool=tool.name,
                    arguments_sha256=decision.normalized_arguments_sha256,
                    reason=decision.reason,
                    suggestions=[item.as_dict() for item in decision.suggestions],
                )
            else:
                request_id = f"permission:{context.invocation_id or tool.name}"
            return ToolPause(
                "approval",
                request_id,
                {
                    "tool": tool.name,
                    "reason": decision.reason,
                    "reason_code": decision.reason_code,
                    "arguments": arguments,
                    "suggestions": [item.as_dict() for item in decision.suggestions],
                },
                {"permission_source": decision.source},
            )
        return None

    async def after(self, tool, arguments, policy, outcome, context):
        del tool, arguments, policy, context
        return outcome


def _audit_rule(rule: PermissionRule) -> dict[str, object]:
    constraints = json.loads(json.dumps(rule.constraints))
    if "command" in constraints:
        constraints["command_sha256"] = hashlib.sha256(
            json.dumps(constraints.pop("command"), sort_keys=True).encode("utf-8")
        ).hexdigest()
    return {
        "id": rule.identifier,
        "behavior": rule.behavior.value,
        "tool": rule.tool,
        "source": rule.source,
        "matcher_version": rule.matcher_version,
        "constraints_summary": constraints,
    }


def _audit_suggestions(
    suggestions: tuple[PermissionUpdate, ...],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for suggestion in suggestions:
        item = suggestion.as_dict()
        constraints = dict(item.get("constraints", {}))
        if "command" in constraints:
            constraints["command_sha256"] = hashlib.sha256(
                str(constraints.pop("command")).encode("utf-8")
            ).hexdigest()
        item["constraints"] = constraints
        result.append(item)
    return result


__all__ = ["PermissionMiddleware"]
