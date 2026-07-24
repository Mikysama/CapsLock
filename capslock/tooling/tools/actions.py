"""Bridge direct-capability tools into durable Actions."""

from __future__ import annotations

from typing import Any

from ...domain import ActionRecord, ActionStatus, ActionType
from ..contracts import (
    ExecutionContext,
    ToolExecution,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
)


def action_data(action: ActionRecord) -> dict[str, object]:
    return {
        "action_id": action.id,
        "kind": action.type.value,
        "summary": action.summary,
        "status": action.status.value,
        "result_kind": action.result_kind.value if action.result_kind else None,
        "request": action.request,
        "result": action.result,
        "error": action.error_message,
    }


async def execute_action_tool(
    context: ExecutionContext, action_type: ActionType, arguments: dict[str, Any]
) -> ToolExecution:
    """Execute a direct-capability tool through the internal Action workflow."""
    payload = dict(arguments)
    if getattr(context, "runtime_state", {}).get("force_manual_approval") is True:
        payload["force_manual_approval"] = True
    action = await context.actions.propose(action_type, **payload)
    data = action_data(action)
    if action.status is ActionStatus.COMPLETED:
        return ToolOutcome.success(data)
    if action.status in {
        ActionStatus.PENDING,
        ActionStatus.APPROVED,
        ActionStatus.RUNNING,
    }:
        return ToolPause(
            "approval",
            action.id,
            data,
            {
                "action_id": action.id,
                "action_type": action.type.value,
            },
        )
    if action.status is ActionStatus.REJECTED:
        return ToolOutcome(
            ToolOutcomeStatus.DENIED,
            False,
            data=data,
            error=action.error_message or "action was rejected",
            error_code="action_rejected",
        )
    if action.status is ActionStatus.CANCELLED:
        return ToolOutcome(
            ToolOutcomeStatus.CANCELLED,
            False,
            data=data,
            error=action.error_message or "action was cancelled",
            error_code="action_cancelled",
        )
    return ToolOutcome.failure(
        action.error_message or "action execution failed",
        code="action_failed",
        executed=True,
        data=data,
    )


__all__ = ["action_data", "execute_action_tool"]
