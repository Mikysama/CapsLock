"""Preparation and execution lifecycle for one model tool invocation."""

from __future__ import annotations

import asyncio
import json
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from ..domain import AgentEventKind, RunStepKind, RunStepStatus
from ..ports import RunJournal
from ..tooling.contracts import (
    DeliveryStatus,
    ExecutionContext,
    ResolvedToolPolicy,
    ToolContent,
    ToolEvent,
    ToolEventKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
)
from ..tooling.executor import ToolRuntime
from ..tooling.presentation import tool_presentation
from .governance import RunGovernor
from .model import ModelToolCall
from ..external import assess_prompt_injection


class InvocationPreparer:
    def __init__(
        self,
        *,
        journal: RunJournal,
        tools: ToolRuntime,
        context_factory: Callable[[str], ExecutionContext],
        outcome_factory: Callable[..., Any],
        paused_error: type[RuntimeError],
    ) -> None:
        self.journal = journal
        self.tools = tools
        self.context_factory = context_factory
        self._outcome_factory = outcome_factory
        self._paused_error = paused_error

    async def prepare(
        self,
        call: ModelToolCall,
        *,
        messages: list[dict[str, object]] | None = None,
        run_id: str,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
        governor: RunGovernor | None,
        cancellation_result: bool = False,
    ) -> Any:
        step = await self.journal.create_step(run_id, RunStepKind.TOOL)
        context = replace(self.context_factory(run_id), governor=governor)
        contract = self.tools.contract(call.name)
        try:
            display_arguments = json.loads(call.arguments)
            if not isinstance(display_arguments, dict):
                display_arguments = {}
        except json.JSONDecodeError:
            display_arguments = {}
        await emit(
            AgentEventKind.TOOL_QUEUED,
            {
                "name": call.name,
                "tool_call_id": call.id,
                "presentation": tool_presentation(call.name, display_arguments),
            },
        )
        await emit(
            AgentEventKind.TOOL_RUNNING,
            {
                "name": call.name,
                "tool_call_id": call.id,
                "presentation": tool_presentation(call.name, display_arguments),
            },
        )
        arguments, result_text, duration_ms, ok = {}, "", 0, False
        event_data: dict[str, object] = {}
        attempt_id: int | None = None
        artifact_id: str | None = None
        outcome = ToolOutcome.failure("tool did not execute", code="tool_not_executed")
        resolved_policy = ResolvedToolPolicy()
        parse_error: Exception | None = None
        try:
            arguments = json.loads(call.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            arguments = {}
            parse_error = exc
        invocation_id = await self.journal.start_tool_invocation(
            run_id=run_id,
            session_id=context.session_id,
            tool_call_id=call.id,
            name=call.name,
            spec=contract.metadata() if contract is not None else {"name": call.name},
            capabilities={},
            arguments=arguments,
            status="validating",
        )
        context = replace(context, invocation_id=invocation_id, catalog=self.tools)
        try:
            if parse_error is not None:
                if governor is not None:
                    attempt_id, _, _ = await governor.before_tool(
                        call.name, {"invalid_arguments": call.arguments}
                    )
                outcome = ToolOutcome.failure(
                    f"invalid tool arguments: {parse_error}",
                    code="invalid_tool_arguments",
                )
            elif contract is None:
                outcome = ToolOutcome.failure(
                    f"unsupported tool: {call.name}", code="unsupported_tool"
                )
            else:
                if governor is not None:
                    attempt_id, _, _ = await governor.before_tool(call.name, arguments)

                async def report(event: ToolEvent) -> None:
                    data = dict(event.data)
                    await emit(
                        AgentEventKind.TOOL_PROGRESS,
                        {
                            "name": call.name,
                            "tool_call_id": call.id,
                            "event": event.kind.value,
                            **data,
                        },
                    )
                    phase = data.get("phase")
                    if event.kind is ToolEventKind.PHASE and isinstance(phase, str):
                        await self.journal.update_tool_invocation(
                            invocation_id,
                            status=phase
                            if phase
                            in {"validating", "authorizing", "queued", "running"}
                            else "running",
                        )

                async def permission_emit(data: dict[str, object]) -> None:
                    await emit(
                        AgentEventKind.TOOL_PERMISSION,
                        {
                            "name": call.name,
                            "tool_call_id": call.id,
                            **data,
                        },
                    )

                context.runtime_state["permission_emit"] = permission_emit

                invocation = self.tools.invoke(
                    call.name, context, arguments, reporter=report
                )
                timeout = governor.remaining_seconds() if governor else None
                async with asyncio.timeout(timeout):
                    invocation_result = await invocation
                arguments = invocation_result.arguments
                resolved_policy = invocation_result.policy
                duration_ms = invocation_result.timings_ms.get("total", 0)
                await self.journal.update_tool_invocation(
                    invocation_id,
                    policy=resolved_policy.as_dict(),
                    timings=invocation_result.timings_ms,
                )
                if isinstance(invocation_result.execution, ToolPause):
                    pause = invocation_result.execution
                    await self.journal.pause_tool_invocation(
                        invocation_id,
                        kind=pause.kind,
                        request_id=pause.request_id,
                        continuation={
                            "arguments": arguments,
                            "resume_data": pause.resume_data,
                        },
                    )
                    if pause.kind == "user_input":
                        await self.journal.create_input_request(
                            request_id=pause.request_id,
                            session_id=context.session_id,
                            run_id=run_id,
                            invocation_id=invocation_id,
                            questions=pause.payload.get("questions", []),
                            resume_data=pause.resume_data,
                        )
                    await self.journal.pause_step(
                        step.id,
                        kind=pause.kind,
                        checkpoint={
                            "messages": list(messages or []),
                            "tool_call_id": call.id,
                            "name": call.name,
                            "arguments": arguments,
                            "request_id": pause.request_id,
                            "resume_data": pause.resume_data,
                        },
                    )
                    raise self._paused_error(
                        pause,
                        invocation_id=invocation_id,
                        step_id=step.id,
                        tool_call_id=call.id,
                    )
                outcome = invocation_result.execution

            if governor is not None and outcome.external_usage:
                await governor.record_external_usage(**outcome.external_usage)
            event_data = outcome.event_data or {}
            audit_outcome = outcome
            if resolved_policy.open_world and outcome.executed:
                source = outcome.content_source or call.name
                raw_payload = json.dumps(
                    {
                        "source": source,
                        "data": outcome.data,
                        "content": [item.as_dict() for item in outcome.content],
                    },
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                ).encode("utf-8")
                assessment = assess_prompt_injection(
                    raw_payload.decode("utf-8", errors="replace")
                )
                outcome = replace(
                    outcome,
                    content_trust=(
                        outcome.content_trust
                        if outcome.content_trust != "tool_data"
                        else "untrusted_external"
                    ),
                    content_source=source,
                    suspicious=assessment.suspicious,
                    risk_signals=assessment.risk_signals,
                )
                audit_outcome = outcome
                if assessment.suspicious:
                    digest = hashlib.sha256(raw_payload).hexdigest()
                    if context.artifacts is None:
                        outcome = ToolOutcome.failure(
                            "suspicious tool content was blocked because quarantine storage is unavailable",
                            code="quarantine_unavailable",
                            executed=True,
                            data={
                                "quarantined": True,
                                "source": source,
                                "bytes": len(raw_payload),
                                "sha256": digest,
                                "risk_signals": list(assessment.risk_signals),
                                "content_available": False,
                            },
                        )
                        outcome = replace(
                            outcome,
                            content_trust="untrusted_external",
                            content_source=source,
                            suspicious=True,
                            risk_signals=assessment.risk_signals,
                            delivery_status=DeliveryStatus.DELIVERY_FAILED,
                        )
                    else:
                        try:
                            artifact = await context.artifacts.put(
                                session_id=context.session_id,
                                run_id=run_id,
                                invocation_id=invocation_id,
                                content=raw_payload,
                            )
                        except Exception:
                            outcome = ToolOutcome.failure(
                                "suspicious tool content could not be quarantined",
                                code="quarantine_failed",
                                executed=True,
                                data={
                                    "quarantined": True,
                                    "source": source,
                                    "bytes": len(raw_payload),
                                    "sha256": digest,
                                    "risk_signals": list(assessment.risk_signals),
                                    "content_available": False,
                                },
                            )
                            outcome = replace(
                                outcome,
                                content_trust="untrusted_external",
                                content_source=source,
                                suspicious=True,
                                risk_signals=assessment.risk_signals,
                                delivery_status=DeliveryStatus.DELIVERY_FAILED,
                            )
                        else:
                            artifact_id = artifact.id
                            descriptor = {
                                "quarantined": True,
                                "source": source,
                                "bytes": len(raw_payload),
                                "sha256": artifact.sha256,
                                "risk_signals": list(assessment.risk_signals),
                                "artifact_id": artifact.id,
                                "read_with": "read_tool_artifact",
                            }
                            outcome = replace(
                                outcome,
                                data=descriptor,
                                content=(ToolContent.artifact(descriptor),),
                                delivery_status=DeliveryStatus.ARTIFACT,
                            )
            result_text = outcome.for_model()
            encoded = result_text.encode("utf-8")
            if contract is not None and len(encoded) > contract.inline_result_bytes:
                captured = encoded[: contract.max_capture_bytes]
                truncated = len(encoded) > len(captured)
                if context.artifacts is not None:
                    try:
                        artifact = await context.artifacts.put(
                            session_id=context.session_id,
                            run_id=run_id,
                            invocation_id=invocation_id,
                            content=captured,
                        )
                    except Exception as exc:
                        preview = captured[:4096].decode("utf-8", errors="replace")
                        outcome = replace(
                            outcome,
                            data={
                                "preview": preview,
                                "original_bytes": len(encoded),
                                "truncated": True,
                                "warning": f"artifact delivery failed: {type(exc).__name__}",
                            },
                            delivery_status=DeliveryStatus.DELIVERY_FAILED,
                        )
                    else:
                        artifact_id = artifact.id
                        descriptor = {
                            "artifact_id": artifact.id,
                            "sha256": artifact.sha256,
                            "captured_bytes": artifact.size_bytes,
                            "original_bytes": len(encoded),
                            "preview": artifact.preview,
                            "truncated": truncated,
                            "read_with": "read_tool_artifact",
                        }
                        outcome = replace(
                            outcome,
                            data=descriptor,
                            content=(ToolContent.artifact(descriptor),),
                            delivery_status=(
                                DeliveryStatus.TRUNCATED
                                if truncated
                                else DeliveryStatus.ARTIFACT
                            ),
                        )
                else:
                    preview = captured[:4096].decode("utf-8", errors="replace")
                    outcome = replace(
                        outcome,
                        data={
                            "preview": preview,
                            "original_bytes": len(encoded),
                            "truncated": True,
                            "warning": "tool artifact storage is unavailable",
                        },
                        delivery_status=DeliveryStatus.DELIVERY_FAILED,
                    )
                result_text = outcome.for_model()
            if outcome.delivery_status is not DeliveryStatus.INLINE and hasattr(
                self.journal, "store_result_replacement"
            ):
                await self.journal.store_result_replacement(
                    tool_call_id=call.id,
                    session_id=context.session_id,
                    invocation_id=invocation_id,
                    delivery_status=outcome.delivery_status.value,
                    replacement=json.loads(result_text),
                )
            ok = outcome.ok
            audit_arguments = (
                arguments
                if audit_outcome.audit_arguments is None
                else audit_outcome.audit_arguments
            )
            audit_text = audit_outcome.for_audit()
            if len(audit_text) > 100_000:
                audit_text = audit_text[:100_000] + "…[audit truncated]"
            await self.journal.record_tool_call(
                run_id,
                call.name,
                audit_arguments,
                ok,
                audit_text,
                duration_ms,
            )
            if governor is not None and attempt_id is not None:
                await governor.finish_tool(attempt_id, ok=ok, duration_ms=duration_ms)
            await self.journal.finish_tool_invocation(
                invocation_id,
                status=(
                    "completed"
                    if outcome.status is ToolOutcomeStatus.SUCCEEDED
                    else "cancelled"
                    if outcome.status is ToolOutcomeStatus.CANCELLED
                    else "failed"
                ),
                execution_status=outcome.status.value,
                delivery_status=outcome.delivery_status.value,
                result_preview=result_text,
                duration_ms=duration_ms,
                artifact_id=artifact_id,
                error_code=outcome.error_code,
            )
        except self._paused_error:
            raise
        except asyncio.CancelledError:
            outcome = ToolOutcome(
                ToolOutcomeStatus.CANCELLED,
                False,
                error="tool execution cancelled",
                error_code="cancelled",
            )
            await self.journal.finish_tool_invocation(
                invocation_id,
                status="cancelled",
                execution_status="cancelled",
                delivery_status=DeliveryStatus.INLINE.value,
                result_preview=outcome.for_model(),
                duration_ms=duration_ms,
                error_code="cancelled",
            )
            await self.journal.finish_step(
                step.id,
                status=RunStepStatus.CANCELLED,
                error="tool execution cancelled",
            )
            if cancellation_result:
                return self._outcome_factory(
                    call,
                    step,
                    arguments,
                    outcome.for_model(),
                    duration_ms,
                    False,
                    {},
                    attempt_id,
                    outcome,
                    resolved_policy,
                    None,
                    invocation_id,
                    run_id,
                    True,
                    False,
                )
            raise
        except BaseException as exc:
            await self.journal.finish_tool_invocation(
                invocation_id,
                status="failed",
                execution_status="failed",
                delivery_status=DeliveryStatus.DELIVERY_FAILED.value,
                result_preview=str(exc) or type(exc).__name__,
                duration_ms=duration_ms,
                error_code=type(exc).__name__,
            )
            await self.journal.finish_step(
                step.id,
                status=RunStepStatus.FAILED,
                error=str(exc) or type(exc).__name__,
            )
            raise
        return self._outcome_factory(
            call,
            step,
            arguments,
            result_text,
            duration_ms,
            ok,
            event_data,
            attempt_id,
            outcome,
            resolved_policy,
            artifact_id,
            invocation_id,
            run_id,
            False,
            context.runtime_state.get("interrupt_pending") is True,
        )


__all__ = ["InvocationPreparer"]
