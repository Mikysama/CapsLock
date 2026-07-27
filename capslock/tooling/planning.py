"""Non-bypassable Plan Mode capability boundary."""

from __future__ import annotations

from .contracts import (
    PlanToolVisibility,
    ToolOutcome,
    ToolOutcomeStatus,
)


class PlanningBoundaryMiddleware:
    """Restrict active planning sessions before ordinary permission decisions."""

    async def normalize(self, tool, arguments, context):
        del tool, context
        return arguments

    async def pre_authorize(self, tool, arguments, context):
        del arguments
        planning = context.planning
        active = bool(
            planning is not None
            and await planning.is_active(context.session_id)
        )
        visibility = tool.contract.plan_visibility
        if active and visibility is PlanToolVisibility.HIDDEN:
            return _denied("tool is unavailable while Plan Mode is active")
        if visibility is PlanToolVisibility.CONTROL:
            allowed = (
                tool.name == "enter_plan_mode" and not active
            ) or (
                active
                and tool.name
                in {"get_plan", "update_plan", "submit_plan", "ask_user"}
            )
            if not allowed:
                return _denied(
                    "plan control tool is unavailable in the current state"
                )
        return None

    async def authorize(self, tool, arguments, policy, context):
        del arguments
        planning = context.planning
        active = bool(
            planning is not None
            and await planning.is_active(context.session_id)
        )
        visibility = tool.contract.plan_visibility

        if visibility is PlanToolVisibility.CONTROL:
            allowed = (
                tool.name == "enter_plan_mode" and not active
            ) or (
                active
                and tool.name
                in {"get_plan", "update_plan", "submit_plan", "ask_user"}
            )
            if allowed:
                context.runtime_state["planning_control_authorized"] = True
                return None
            return _denied("plan control tool is unavailable in the current state")

        if not active:
            return None
        if visibility is not PlanToolVisibility.LOCAL_READ:
            return _denied("tool is unavailable while Plan Mode is active")
        if (
            not policy.read_only
            or policy.destructive
            or policy.external_side_effects
            or policy.open_world
            or policy.context_mutation
        ):
            return _denied("tool is not deterministically local and read-only")
        return None

    async def after(self, tool, arguments, policy, outcome, context):
        del tool, arguments, policy, context
        return outcome


def _denied(reason: str) -> ToolOutcome:
    return ToolOutcome(
        ToolOutcomeStatus.DENIED,
        False,
        error=reason,
        error_code="plan_mode_read_only",
    )


__all__ = ["PlanningBoundaryMiddleware"]
