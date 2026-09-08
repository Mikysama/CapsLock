"""Model-facing delegation tool for local child Agents."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from ...collaboration.models import (
    AgentTaskContract,
    CapabilityGrant,
    CapabilityKind,
    VerificationRequirement,
    MailboxMessageKind,
)
from ...external import assess_prompt_injection
from ..contracts import (
    ExecutionContext,
    InterruptBehavior,
    ResolvedToolPolicy,
    ToolDefinition,
    ToolOutcome,
    ToolOutcomeStatus,
    define_tool,
)


def _outcome(
    ok: bool, data: object, error: str | None = None, **values: Any
) -> ToolOutcome:
    return ToolOutcome(
        ToolOutcomeStatus.SUCCEEDED if ok else ToolOutcomeStatus.FAILED,
        ok,
        data=data,
        error=error,
        error_code=None if ok else "tool_failed",
        **values,
    )


def delegation_tool() -> ToolDefinition:
    return define_tool(
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
                            "memory_namespace": {
                                "type": "string",
                                "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
                            },
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
                },
                "background": {"type": "boolean"},
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
        _delegate,
        policy=ResolvedToolPolicy(
            context_mutation=True,
            external_side_effects=True,
            open_world=True,
            interrupt_behavior=InterruptBehavior.COMPLETE,
        ),
    )


CHILD_MAILBOX_TOOL_NAMES = frozenset(
    {
        "read_parent_messages",
        "send_parent_message",
        "ack_parent_message",
        "send_team_message",
    }
)


def child_mailbox_tools(
    collaboration: Any, contract: AgentTaskContract
) -> list[ToolDefinition]:
    """Bind a child-only mailbox view to one immutable task contract."""
    if collaboration is None or not collaboration.mailbox_enabled:
        return []

    async def read_parent_messages(
        _context: ExecutionContext, _arguments: dict[str, Any]
    ) -> ToolOutcome:
        values = await collaboration.read_child_messages(
            contract.task_id, parent_run_id=contract.parent_run_id
        )
        return ToolOutcome.success({"messages": values})

    async def send_parent_message(
        context: ExecutionContext, arguments: dict[str, Any]
    ) -> ToolOutcome:
        kind = MailboxMessageKind(str(arguments["kind"]))
        payload = dict(arguments["payload"])
        if kind is MailboxMessageKind.ARTIFACT_OFFER:
            requested = str(payload.get("path", ""))
            path = context.policy.resolve(requested)
            if not path.is_file():
                raise ValueError("offered child artifact is not a regular file")
            size = path.stat().st_size
            if size > contract.verification_requirements.max_artifact_bytes:
                raise ValueError(
                    "offered child artifact exceeds the contract size limit"
                )
            payload = {
                "path": str(path.relative_to(context.policy.root)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": size,
                **(
                    {"summary": str(payload["summary"])[:1000]}
                    if payload.get("summary")
                    else {}
                ),
            }
        value = await collaboration.send_child_message(
            contract.task_id,
            parent_run_id=contract.parent_run_id,
            kind=kind,
            payload=payload,
        )
        return ToolOutcome.success(value)

    async def acknowledge_parent_message(
        _context: ExecutionContext, arguments: dict[str, Any]
    ) -> ToolOutcome:
        await collaboration.acknowledge_child_message(
            str(arguments["message_id"]),
            task_id=contract.task_id,
            parent_run_id=contract.parent_run_id,
        )
        return ToolOutcome.success({"acknowledged": True})

    async def send_team_message(
        _context: ExecutionContext, arguments: dict[str, Any]
    ) -> ToolOutcome:
        value = await collaboration.send_team_message(
            source_task_id=contract.task_id,
            source_parent_run_id=contract.parent_run_id,
            recipient_agent_id=arguments.get("recipient_agent_id"),
            broadcast=bool(arguments.get("broadcast", False)),
            payload=dict(arguments["payload"]),
        )
        return ToolOutcome.success(value)

    return [
        define_tool(
            "read_parent_messages",
            "Read new instructions, responses, or cancellation notices from the parent Agent.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            read_parent_messages,
            policy=ResolvedToolPolicy(read_only=True, open_world=True),
        ),
        define_tool(
            "send_parent_message",
            "Send a bounded question, progress update, response, or artifact offer to the parent Agent.",
            {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["question", "progress", "response", "artifact_offer"],
                    },
                    "payload": {"type": "object"},
                },
                "required": ["kind", "payload"],
                "additionalProperties": False,
            },
            send_parent_message,
            policy=ResolvedToolPolicy(context_mutation=True),
        ),
        define_tool(
            "ack_parent_message",
            "Acknowledge one delivered parent mailbox message.",
            {
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
                "additionalProperties": False,
            },
            acknowledge_parent_message,
            policy=ResolvedToolPolicy(context_mutation=True),
        ),
        define_tool(
            "send_team_message",
            "Send untrusted task data to one teammate or all active teammates in the same team.",
            {
                "type": "object",
                "properties": {
                    "recipient_agent_id": {"type": "string"},
                    "broadcast": {"type": "boolean"},
                    "payload": {"type": "object"},
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
            send_team_message,
            policy=ResolvedToolPolicy(context_mutation=True),
        ),
    ]


async def _delegate(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    service = context.collaboration
    if service is None:
        return _outcome(False, {}, "multi-Agent collaboration is not configured")
    raw_tasks = arguments.get("tasks")
    if not isinstance(raw_tasks, list):
        return _outcome(False, {}, "tasks must be an array")
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
                    memory_namespace=item.get("memory_namespace"),
                )
            )
        contracts = await _reserve_parent_budget(context, contracts)
        background = bool(arguments.get("background", False))
        outputs = await service.delegate(contracts, background=background)
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return _outcome(False, {}, str(exc))
    raw_tasks = [output.as_dict() for output in outputs]
    model_tasks: list[dict[str, Any]] = []
    for output, raw in zip(outputs, raw_tasks, strict=True):
        assessment = assess_prompt_injection(output.summary)
        if not assessment.suspicious:
            model_tasks.append(raw)
            continue
        encoded = output.summary.encode("utf-8")
        descriptor: dict[str, Any] = {
            "quarantined": True,
            "source": f"child_agent_summary:{output.task_id}",
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "risk_signals": list(assessment.risk_signals),
        }
        if context.artifacts is None or context.invocation_id is None:
            descriptor.update(
                {"content_available": False, "error_code": "quarantine_unavailable"}
            )
        else:
            try:
                artifact = await context.artifacts.put(
                    session_id=context.session_id,
                    run_id=context.run_id,
                    invocation_id=context.invocation_id,
                    content=encoded,
                    media_type="text/plain",
                )
            except Exception:
                descriptor.update(
                    {"content_available": False, "error_code": "quarantine_failed"}
                )
            else:
                descriptor.update(
                    {
                        "artifact_id": artifact.id,
                        "sha256": artifact.sha256,
                        "read_with": "read_tool_artifact",
                    }
                )
        model_tasks.append({**raw, "summary": descriptor})
    data = {
        "tasks": model_tasks,
        "background": background,
    }
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
    return _outcome(
        True,
        data,
        content_trust="untrusted_agent",
        content_source="child_agent_outputs",
        audit_data={"tasks": raw_tasks, "background": background},
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


def agent_control_tools() -> list[ToolDefinition]:
    safe_read = ResolvedToolPolicy(
        read_only=True,
        open_world=True,
        interrupt_behavior=InterruptBehavior.CANCEL,
    )
    return [
        define_tool(
            "get_agent_task",
            "Read or briefly wait for one background child Agent task.",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "wait": {"type": "boolean"},
                    "timeout": {"type": "number", "minimum": 0.1, "maximum": 60},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            _get_agent_task,
            policy=safe_read,
            deferred=True,
            search_hint="background child Agent status wait result",
        ),
        define_tool(
            "stop_agent_task",
            "Cancel an active child Agent task owned by this run.",
            {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
            _stop_agent_task,
            policy=ResolvedToolPolicy(
                context_mutation=True,
                interrupt_behavior=InterruptBehavior.COMPLETE,
            ),
            deferred=True,
            search_hint="cancel stop background child Agent",
        ),
        define_tool(
            "send_agent_message",
            "Send a bounded instruction, response, or cancellation to one owned child Agent task.",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["instruction", "response", "cancel"],
                    },
                    "payload": {"type": "object"},
                },
                "required": ["task_id", "kind", "payload"],
                "additionalProperties": False,
            },
            _send_agent_message,
            policy=ResolvedToolPolicy(
                context_mutation=True,
                interrupt_behavior=InterruptBehavior.COMPLETE,
            ),
            deferred=True,
            search_hint="message instruct answer child Agent",
        ),
        define_tool(
            "read_agent_messages",
            "Read queued questions, progress, responses, and artifact offers from one owned child task.",
            {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
            _read_agent_messages,
            policy=safe_read,
            deferred=True,
            search_hint="mailbox question progress child Agent",
        ),
        define_tool(
            "ack_agent_message",
            "Acknowledge one delivered child Agent mailbox message.",
            {
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
                "additionalProperties": False,
            },
            _ack_agent_message,
            policy=ResolvedToolPolicy(context_mutation=True),
            deferred=True,
            search_hint="acknowledge child Agent message",
        ),
        define_tool(
            "publish_agent_artifact",
            "Publish one verified child artifact through allowlist, digest, and parent-baseline checks.",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "path": {"type": "string"},
                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                "required": ["task_id", "path", "sha256"],
                "additionalProperties": False,
            },
            _publish_agent_artifact,
            policy=ResolvedToolPolicy(
                context_mutation=True,
                external_side_effects=True,
                interrupt_behavior=InterruptBehavior.COMPLETE,
            ),
            deferred=True,
            search_hint="publish promote child Agent artifact",
        ),
        define_tool(
            "create_agent_team",
            "Create a session-scoped Agent team.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            _create_agent_team,
            policy=ResolvedToolPolicy(context_mutation=True),
            deferred=True,
            search_hint="create named Agent team",
        ),
        define_tool(
            "start_agent",
            "Start one named persistent Agent in a session team.",
            {
                "type": "object",
                "properties": {
                    "team_id": {"type": "string"},
                    "name": {"type": "string"},
                    "profile": {"type": "object"},
                    "workspace_mode": {
                        "type": "string",
                        "enum": ["snapshot", "worktree", "shared_read"],
                    },
                },
                "required": ["team_id", "name"],
                "additionalProperties": False,
            },
            _start_agent,
            policy=ResolvedToolPolicy(context_mutation=True),
            deferred=True,
            search_hint="start named persistent Agent worker",
        ),
        define_tool(
            "create_agent_task",
            "Create one immutable task in an Agent-team dependency graph.",
            _team_task_schema(),
            _create_agent_task,
            policy=ResolvedToolPolicy(context_mutation=True),
            deferred=True,
            search_hint="create Agent DAG task dependency",
        ),
        define_tool(
            "assign_agent_task",
            "Atomically claim and run a ready Agent-team task.",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                },
                "required": ["task_id", "agent_id"],
                "additionalProperties": False,
            },
            _assign_agent_task,
            policy=ResolvedToolPolicy(context_mutation=True),
            deferred=True,
            search_hint="claim assign run Agent task",
        ),
        define_tool(
            "follow_up_agent",
            "Queue a new immutable task for a named persistent Agent.",
            {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "objective": {"type": "string"},
                    "input_context": {"type": "object"},
                    "plan_task_id": {"type": "string"},
                },
                "required": ["agent_id", "objective"],
                "additionalProperties": False,
            },
            _follow_up_agent,
            policy=ResolvedToolPolicy(context_mutation=True),
            deferred=True,
            search_hint="follow up resume persistent Agent",
        ),
        define_tool(
            "resume_agent",
            "Explicitly resume an interrupted named Agent task.",
            {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "task_id": {"type": "string"},
                },
                "required": ["agent_id"],
                "additionalProperties": False,
            },
            _resume_agent,
            policy=ResolvedToolPolicy(context_mutation=True),
            deferred=True,
            search_hint="resume interrupted Agent checkpoint",
        ),
        define_tool(
            "get_agent_team",
            "Inspect a session-owned Agent team, tasks, attempts, approvals, and budget ledger.",
            {
                "type": "object",
                "properties": {"team_id": {"type": "string"}},
                "required": ["team_id"],
                "additionalProperties": False,
            },
            _get_agent_team,
            policy=safe_read,
            deferred=True,
            search_hint="Agent team DAG status budget approvals",
        ),
        define_tool(
            "stop_agent",
            "Stop a session-owned persistent Agent and cancel its active task.",
            {
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
                "additionalProperties": False,
            },
            _stop_agent,
            policy=ResolvedToolPolicy(context_mutation=True),
            deferred=True,
            search_hint="stop persistent Agent worker",
        ),
        define_tool(
            "send_team_message",
            "Send an instruction to one named Agent or broadcast within a session team.",
            {
                "type": "object",
                "properties": {
                    "team_id": {"type": "string"},
                    "recipient_agent_id": {"type": "string"},
                    "broadcast": {"type": "boolean"},
                    "payload": {"type": "object"},
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
            _send_team_message,
            policy=ResolvedToolPolicy(context_mutation=True),
            deferred=True,
            search_hint="message broadcast Agent teammate",
        ),
    ]


def _team_task_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "team_id": {"type": "string"},
            "objective": {"type": "string"},
            "input_context": {"type": "object"},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "assignee_agent_id": {"type": "string"},
            "plan_task_id": {"type": "string"},
            "priority": {"type": "integer"},
            "allowed_paths": {"type": "array", "items": {"type": "string"}},
            "capabilities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [item.value for item in CapabilityKind],
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
            "verification_requirements": {"type": "object"},
        },
        "required": ["team_id", "objective"],
        "additionalProperties": False,
    }


def _team_contract(
    context: ExecutionContext, arguments: dict[str, Any]
) -> AgentTaskContract:
    grants = tuple(
        CapabilityGrant(
            CapabilityKind(str(item["kind"])),
            scope=item.get("scope"),
            plugin=item.get("plugin"),
        )
        for item in arguments.get("capabilities", ())
    )
    return AgentTaskContract.create(
        context.run_id,
        str(arguments["objective"]),
        input_context=dict(arguments.get("input_context") or {}),
        allowed_paths=tuple(arguments.get("allowed_paths") or ()),
        capabilities=grants,
        model_profile=arguments.get("model_profile"),
        limits=dict(arguments.get("limits") or {"max_tool_rounds": 16}),
        verification_requirements=_verification_requirements(
            arguments.get("verification_requirements")
        ),
    )


async def _create_agent_team(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    value = await context.collaboration.create_team(
        context.session_id,
        str(arguments["name"]),
        created_by_run_id=context.run_id,
    )
    return ToolOutcome.success(value)


async def _start_agent(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    value = await context.collaboration.start_agent(
        session_id=context.session_id,
        team_id=str(arguments["team_id"]),
        name=str(arguments["name"]),
        profile=dict(arguments.get("profile") or {}),
        workspace_mode=(
            str(arguments["workspace_mode"])
            if arguments.get("workspace_mode") is not None
            else None
        ),
    )
    return ToolOutcome.success(value)


async def _create_agent_task(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    contract = (
        await _reserve_parent_budget(context, [_team_contract(context, arguments)])
    )[0]
    value = await context.collaboration.create_agent_task(
        contract,
        session_id=context.session_id,
        team_id=str(arguments["team_id"]),
        depends_on=tuple(str(item) for item in arguments.get("depends_on", ())),
        worker_id=arguments.get("assignee_agent_id"),
        plan_task_id=arguments.get("plan_task_id"),
        priority=int(arguments.get("priority", 0)),
    )
    return ToolOutcome.success(value)


async def _assign_agent_task(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    value = await context.collaboration.assign_agent_task(
        str(arguments["task_id"]),
        str(arguments["agent_id"]),
        session_id=context.session_id,
    )
    return ToolOutcome.success(value)


async def _follow_up_agent(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    contract = (
        await _reserve_parent_budget(
            context,
            [
                AgentTaskContract.create(
                    context.run_id,
                    str(arguments["objective"]),
                    input_context=dict(arguments.get("input_context") or {}),
                )
            ],
        )
    )[0]
    value = await context.collaboration.follow_up_agent(
        str(arguments["agent_id"]),
        contract,
        session_id=context.session_id,
        plan_task_id=arguments.get("plan_task_id"),
    )
    return ToolOutcome.success(value)


async def _resume_agent(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    value = await context.collaboration.resume_agent(
        str(arguments["agent_id"]),
        session_id=context.session_id,
        task_id=arguments.get("task_id"),
    )
    return ToolOutcome.success(value)


async def _get_agent_team(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    value = await context.collaboration.get_team(
        str(arguments["team_id"]), session_id=context.session_id
    )
    return ToolOutcome.success(value)


async def _stop_agent(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    value = await context.collaboration.stop_agent(
        str(arguments["agent_id"]), session_id=context.session_id
    )
    return ToolOutcome.success(value)


async def _send_team_message(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    value = await context.collaboration.send_team_message(
        session_id=context.session_id,
        team_id=arguments.get("team_id"),
        recipient_agent_id=arguments.get("recipient_agent_id"),
        broadcast=bool(arguments.get("broadcast", False)),
        payload=dict(arguments["payload"]),
    )
    return ToolOutcome.success(value)


async def _send_agent_message(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    value = await context.collaboration.send_message(
        str(arguments["task_id"]),
        session_id=context.session_id,
        kind=MailboxMessageKind(str(arguments["kind"])),
        payload=dict(arguments["payload"]),
    )
    return ToolOutcome.success(value)


async def _read_agent_messages(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    values = await context.collaboration.read_messages(
        str(arguments["task_id"]), session_id=context.session_id
    )
    return ToolOutcome.success({"messages": values})


async def _ack_agent_message(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    await context.collaboration.acknowledge_message(
        str(arguments["message_id"]), session_id=context.session_id
    )
    return ToolOutcome.success({"acknowledged": True})


async def _publish_agent_artifact(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure("multi-Agent collaboration is not configured")
    await context.collaboration.publish_artifact(
        str(arguments["task_id"]),
        {"path": str(arguments["path"]), "sha256": str(arguments["sha256"])},
        session_id=context.session_id,
    )
    return ToolOutcome.success({"published": True, "path": arguments["path"]})


async def _get_agent_task(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure(
            "multi-Agent collaboration is not configured", code="agents_unavailable"
        )
    value = await context.collaboration.status(
        str(arguments["task_id"]),
        session_id=context.session_id,
        wait=bool(arguments.get("wait", False)),
        timeout=float(arguments.get("timeout", 60)),
    )
    return ToolOutcome.success(value)


async def _stop_agent_task(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.collaboration is None:
        return ToolOutcome.failure(
            "multi-Agent collaboration is not configured", code="agents_unavailable"
        )
    task_id = str(arguments["task_id"])
    await context.collaboration.status(task_id, session_id=context.session_id)
    await context.collaboration.cancel(task_id)
    value = await context.collaboration.status(task_id, session_id=context.session_id)
    return ToolOutcome.success(value)


async def _reserve_parent_budget(
    context: ExecutionContext, contracts: list[AgentTaskContract]
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
    from ...structured_output import child_agent_result_schema

    child_agent_result_schema(output_schema)
    return VerificationRequirement(
        output_schema=output_schema,
        required_paths=tuple(required_paths),
        max_artifacts=int(value.get("max_artifacts", 20)),
        max_artifact_bytes=int(value.get("max_artifact_bytes", 512_000)),
        required_checks=tuple(required_checks),
    )
