"""Model-facing Plan Mode control tools."""

from __future__ import annotations

from typing import Any

from ..contracts import (
    PlanToolVisibility,
    ResolvedToolPolicy,
    ToolOutcome,
    ToolPause,
    define_tool,
)


def _service(context):
    if context.planning is None:
        raise ValueError("planning service is unavailable")
    return context.planning


async def enter_plan_mode(context, arguments: dict[str, Any]):
    objective = str(arguments.get("objective", "")).strip()
    if not objective:
        raise ValueError("objective must not be empty")
    if await _service(context).is_active(context.session_id):
        current = await _service(context).current(context.session_id)
        assert current is not None
        return ToolOutcome.success(
            {"active": True, "plan_id": current[0].id, "already_active": True}
        )
    if context.invocation_id is None:
        raise ValueError("enter_plan_mode requires a persisted invocation")
    request = await _service(context).repository.create_enter_request(
        context.session_id,
        objective,
        run_id=context.run_id,
        invocation_id=context.invocation_id,
    )
    return ToolPause(
        "approval",
        request.id,
        {
            "request_type": "plan_enter",
            "objective": objective,
            "choices": ["enter", "reject"],
        },
        {"plan_request_kind": "enter"},
    )


async def get_plan(context, arguments: dict[str, Any]):
    del arguments
    current = await _service(context).current(context.session_id)
    if current is None:
        raise ValueError("plan mode is not active")
    plan, revision = current
    return ToolOutcome.success(
        {
            "plan_id": plan.id,
            "objective": plan.objective,
            "status": plan.status.value,
            "revision": revision.ordinal,
            "sha256": revision.sha256,
            "content": revision.content,
        },
        audit_data={
            "plan_id": plan.id,
            "revision": revision.ordinal,
            "sha256": revision.sha256,
        },
    )


async def update_plan(context, arguments: dict[str, Any]):
    content = arguments.get("content")
    digest = arguments.get("expected_sha256")
    if not isinstance(content, str) or not isinstance(digest, str):
        raise ValueError("content and expected_sha256 must be strings")
    plan, revision = await _service(context).update(
        context.session_id,
        content,
        expected_sha256=digest,
        source="model",
        run_id=context.run_id,
    )
    return ToolOutcome.success(
        {
            "plan_id": plan.id,
            "revision": revision.ordinal,
            "sha256": revision.sha256,
        },
        audit_arguments={"expected_sha256": digest},
    )


async def submit_plan(context, arguments: dict[str, Any]):
    digest = arguments.get("expected_sha256")
    if not isinstance(digest, str):
        raise ValueError("expected_sha256 must be a string")
    current = await _service(context).current(context.session_id)
    if current is None:
        raise ValueError("plan mode is not active")
    plan, revision = current
    if context.invocation_id is None:
        raise ValueError("submit_plan requires a persisted invocation")
    request = await _service(context).repository.submit(
        plan.id,
        expected_sha256=digest,
        run_id=context.run_id,
        invocation_id=context.invocation_id,
    )
    return ToolPause(
        "approval",
        request.id,
        {
            "request_type": "plan_submit",
            "plan_id": plan.id,
            "objective": plan.objective,
            "revision": revision.ordinal,
            "sha256": revision.sha256,
            "preview": revision.content,
            "choices": ["implement", "feedback", "reject"],
        },
        {"plan_request_kind": "submit", "plan_sha256": revision.sha256},
    )


def plan_tools():
    string = {"type": "string"}
    control = PlanToolVisibility.CONTROL
    return [
        define_tool(
            "enter_plan_mode",
            "Request user confirmation to enter a local read-only planning mode before designing a complex change.",
            {
                "type": "object",
                "properties": {"objective": string},
                "required": ["objective"],
                "additionalProperties": False,
            },
            enter_plan_mode,
            policy=ResolvedToolPolicy(context_mutation=True),
            plan_visibility=control,
        ),
        define_tool(
            "get_plan",
            "Read the current session plan and its revision hash.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            get_plan,
            policy=ResolvedToolPolicy.safe_read(),
            plan_visibility=control,
        ),
        define_tool(
            "update_plan",
            "Replace the current session plan using a SHA-256 precondition. This is the only workspace-state write available in Plan Mode.",
            {
                "type": "object",
                "properties": {"content": string, "expected_sha256": string},
                "required": ["content", "expected_sha256"],
                "additionalProperties": False,
            },
            update_plan,
            policy=ResolvedToolPolicy(context_mutation=True),
            plan_visibility=control,
        ),
        define_tool(
            "submit_plan",
            "Submit the exact current plan revision for user approval.",
            {
                "type": "object",
                "properties": {"expected_sha256": string},
                "required": ["expected_sha256"],
                "additionalProperties": False,
            },
            submit_plan,
            policy=ResolvedToolPolicy(context_mutation=True),
            plan_visibility=control,
        ),
    ]


__all__ = ["plan_tools"]
