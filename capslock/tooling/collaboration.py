"""Model-facing delegation tool for local child Agents."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..collaboration.models import (
    AgentTaskContract,
    CapabilityGrant,
    CapabilityKind,
    VerificationRequirement,
)
from .async_core import RunContext, Tool, ToolResult


def delegation_tool() -> Tool:
    return Tool(
        "delegate_agents",
        "Delegate up to four independent, explicitly scoped child Agent tasks and return only verified outputs.",
        {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "properties": {
                            "objective": {"type": "string"},
                            "input_context": {"type": "object"},
                            "allowed_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "capabilities": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "kind": {
                                            "type": "string",
                                            "enum": [
                                                item.value for item in CapabilityKind
                                            ],
                                        },
                                        "scope": {"type": "string"},
                                        "plugin": {"type": "string"},
                                    },
                                    "required": ["kind"],
                                    "additionalProperties": False,
                                },
                            },
                            "model_profile": {"type": "string"},
                            "limits": {"type": "object"},
                            "verification_requirements": {
                                "type": "object",
                                "properties": {
                                    "output_schema": {"type": "object"},
                                    "required_paths": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "max_artifacts": {"type": "integer"},
                                    "max_artifact_bytes": {"type": "integer"},
                                    "required_checks": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "additionalProperties": False,
                            },
                        },
                        "required": ["objective"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
        _delegate,
    )


async def _delegate(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    service = context.collaboration
    if service is None:
        return ToolResult(False, {}, "multi-Agent collaboration is not configured")
    raw_tasks = arguments.get("tasks")
    if not isinstance(raw_tasks, list):
        return ToolResult(False, {}, "tasks must be an array")
    contracts: list[AgentTaskContract] = []
    try:
        for item in raw_tasks:
            if not isinstance(item, dict):
                raise ValueError("each child task must be an object")
            grants = []
            for raw in item.get("capabilities", []):
                if not isinstance(raw, dict):
                    raise ValueError("capabilities must be objects")
                grants.append(
                    CapabilityGrant(
                        CapabilityKind(str(raw.get("kind", "workspace_read"))),
                        scope=raw.get("scope"),
                        plugin=raw.get("plugin"),
                    )
                )
            contracts.append(
                AgentTaskContract.create(
                    context.run_id,
                    str(item["objective"]),
                    input_context=item.get("input_context") or {},
                    allowed_paths=tuple(item.get("allowed_paths") or ()),
                    capabilities=tuple(grants),
                    model_profile=item.get("model_profile"),
                    limits=item.get("limits") or {"max_tool_rounds": 16},
                    verification_requirements=_verification_requirements(
                        item.get("verification_requirements")
                    ),
                )
            )
        contracts = await _reserve_parent_budget(context, contracts)
        outputs = await service.delegate(contracts)
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return ToolResult(False, {}, str(exc))
    data = {"tasks": [output.as_dict() for output in outputs]}
    usage = {
        name: sum(float(output.usage.get(name, 0)) for output in outputs)
        for name in ("cost_usd",)
    }
    usage.update(
        {
            name: sum(int(output.usage.get(name, 0)) for output in outputs)
            for name in ("input_tokens", "output_tokens", "tool_rounds", "tool_calls")
        }
    )
    return ToolResult(
        True,
        data,
        event_data={
            "collaboration": {
                "tasks": [
                    {
                        "task_id": output.task_id,
                        "state": output.state.value,
                        "verified": output.verified,
                    }
                    for output in outputs
                ]
            }
        },
        external_usage=usage,
    )


async def _reserve_parent_budget(
    context: RunContext, contracts: list[AgentTaskContract]
) -> list[AgentTaskContract]:
    governor = context.governor
    if governor is None:
        return contracts
    remaining = (await governor.current()).as_dict()["remaining"]
    count = len(contracts)
    rounds = int(remaining["tool_rounds"]) - 1
    if rounds < count:
        raise ValueError(
            "parent run has insufficient tool-round budget for children and summary"
        )
    round_share = max(1, rounds // count)
    token_share = (
        None if remaining["tokens"] is None else int(remaining["tokens"]) // (count + 1)
    )
    call_share = (
        None
        if remaining["tool_calls"] is None
        else int(remaining["tool_calls"]) // count
    )
    cost_share = (
        None
        if remaining["budget_usd"] is None
        else float(remaining["budget_usd"]) / (count + 1)
    )
    duration = remaining["duration_ms"]
    if token_share == 0 or call_share == 0 or cost_share == 0:
        raise ValueError("parent run has insufficient aggregate budget for child tasks")
    reserved = []
    for contract in contracts:
        limits = dict(contract.limits)
        limits["max_tool_rounds"] = min(
            int(limits.get("max_tool_rounds") or 16), round_share
        )
        if token_share is not None:
            limits["max_tokens"] = min(
                int(limits.get("max_tokens") or token_share), token_share
            )
        if call_share is not None:
            limits["max_tool_calls"] = min(
                int(limits.get("max_tool_calls") or call_share), call_share
            )
        if cost_share is not None:
            limits["max_budget_usd"] = min(
                float(limits.get("max_budget_usd") or cost_share), cost_share
            )
        if duration is not None:
            limits["max_duration_ms"] = min(
                int(limits.get("max_duration_ms") or duration), int(duration)
            )
        reserved.append(replace(contract, limits=limits))
    return reserved


def _verification_requirements(value: Any) -> VerificationRequirement:
    if value is None:
        return VerificationRequirement()
    if not isinstance(value, dict):
        raise ValueError("verification_requirements must be an object")
    required_paths = value.get("required_paths", ())
    required_checks = value.get("required_checks", ())
    output_schema = value.get("output_schema", {})
    if not isinstance(required_paths, list) or not all(
        isinstance(item, str) for item in required_paths
    ):
        raise ValueError("verification required_paths must be an array of strings")
    if not isinstance(required_checks, list) or not all(
        isinstance(item, str) for item in required_checks
    ):
        raise ValueError("verification required_checks must be an array of strings")
    if not isinstance(output_schema, dict):
        raise ValueError("verification output_schema must be an object")
    return VerificationRequirement(
        output_schema=output_schema,
        required_paths=tuple(required_paths),
        max_artifacts=int(value.get("max_artifacts", 20)),
        max_artifact_bytes=int(value.get("max_artifact_bytes", 512_000)),
        required_checks=tuple(required_checks),
    )
