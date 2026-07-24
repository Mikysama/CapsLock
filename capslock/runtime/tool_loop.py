"""Single asynchronous streaming model/tool loop."""

from __future__ import annotations

import json
import asyncio
from dataclasses import replace
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..domain import (
    AgentEventKind,
    BudgetSnapshot,
    RunMode,
    RunStepKind,
    RunStepStatus,
    RunStopped,
    StopReason,
)
from ..evidence import Evidence
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
from ..tooling.schema import SchemaValidationError
from ..tooling.presentation import tool_presentation
from .model import (
    ChatModel,
    ModelMessage,
    ModelToolCall,
    ModelUsage,
    stream_model_response,
)
from .governance import RunGovernor


class ToolLoopError(RuntimeError):
    def __init__(
        self, message: str, *, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class ToolLoopPaused(RuntimeError):
    def __init__(
        self,
        pause: ToolPause,
        *,
        invocation_id: str,
        step_id: str,
        tool_call_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        super().__init__(f"tool invocation paused for {pause.kind}")
        self.pause = pause
        self.invocation_id = invocation_id
        self.step_id = step_id
        self.tool_call_id = tool_call_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass(frozen=True)
class ToolLoopResult:
    text: str
    evidence: dict[str, Evidence]
    source_ids: set[str]
    memories: dict[str, object]
    input_tokens: int
    output_tokens: int
    budget: BudgetSnapshot | None = None
    stop_reason: StopReason | None = None


@dataclass
class ToolCallOutcome:
    call: ModelToolCall
    step: object
    arguments: dict[str, Any]
    result_text: str
    duration_ms: int
    ok: bool
    event_data: dict[str, object]
    attempt_id: int | None
    outcome: ToolOutcome
    policy: ResolvedToolPolicy
    artifact_id: str | None = None
    invocation_id: str | None = None
    run_id: str | None = None
    step_finalized: bool = False
    interrupt_after: bool = False


class ModelStepExecutor:
    def __init__(self, *, journal: RunJournal, model: str, tools: ToolRuntime) -> None:
        self.journal = journal
        self.model = model
        self.tools = tools

    async def invoke(
        self,
        *,
        chat_model: ChatModel,
        messages: list[dict[str, object]],
        run_id: str,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
        governor: RunGovernor | None,
    ):
        step = await self.journal.create_step(run_id, RunStepKind.MODEL)
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = ModelUsage()
        try:
            stream = stream_model_response(
                chat_model,
                model=self.model,
                messages=messages,
                tools=self.tools.schemas,
            )
            timeout = governor.remaining_seconds() if governor else None
            async with asyncio.timeout(timeout):
                async for delta in stream:
                    if delta.reasoning:
                        reasoning.append(delta.reasoning)
                        await emit(AgentEventKind.THINKING, {"text": delta.reasoning})
                    if delta.content:
                        content.append(delta.content)
                        await emit(AgentEventKind.TEXT_DELTA, {"text": delta.content})
                    if delta.tool_index is not None:
                        call = calls.setdefault(
                            delta.tool_index,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        if delta.tool_call_id:
                            call["id"] = delta.tool_call_id
                        if delta.tool_name:
                            call["name"] += delta.tool_name
                        call["arguments"] += delta.tool_arguments
                    if delta.usage is not None:
                        usage = delta.usage
        except TimeoutError:
            if governor is not None:
                await governor.stop(StopReason.MAX_DURATION)
            raise
        except Exception as exc:
            await self.journal.finish_step(
                step.id, status=RunStepStatus.FAILED, error=str(exc)
            )
            raise
        message = ModelMessage(
            "".join(content) or None,
            tuple(
                ModelToolCall(
                    item["id"] or f"call_{index}", item["name"], item["arguments"]
                )
                for index, item in sorted(calls.items())
            ),
            "".join(reasoning) or None,
        )
        return step, message, usage


class ToolCallExecutor:
    def __init__(
        self,
        *,
        journal: RunJournal,
        tools: ToolRuntime,
        context_factory: Callable[[str], ExecutionContext],
        aggregate_result_bytes: int = 65_536,
    ) -> None:
        self.journal = journal
        self.tools = tools
        self.context_factory = context_factory
        self.aggregate_result_bytes = max(1024, aggregate_result_bytes)
        self._aggregate_used = 0

    async def prepare(
        self,
        call: ModelToolCall,
        *,
        messages: list[dict[str, object]] | None = None,
        run_id: str,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
        governor: RunGovernor | None,
        cancellation_result: bool = False,
    ) -> ToolCallOutcome:
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
                    attempt_id, _, _ = await governor.before_tool(
                        call.name, arguments
                    )

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
                            if phase in {"validating", "authorizing", "queued", "running"}
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
                    raise ToolLoopPaused(
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
        except ToolLoopPaused:
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
                step.id, status=RunStepStatus.CANCELLED, error="tool execution cancelled"
            )
            if cancellation_result:
                return ToolCallOutcome(
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
        return ToolCallOutcome(
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

    async def commit(
        self,
        outcome: ToolCallOutcome,
        *,
        messages: list[dict[str, object]],
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
        governor: RunGovernor | None,
        evidence: dict[str, Evidence],
        source_ids: set[str],
        memories: dict[str, object],
    ) -> None:
        await self._enforce_aggregate_budget(outcome)
        call = outcome.call
        model_content: object = outcome.result_text
        if outcome.outcome.content:
            model_content = [
                {"type": "text", "value": outcome.result_text},
                *(item.as_dict() for item in outcome.outcome.content),
            ]
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": model_content,
            }
        )
        for passage in outcome.outcome.citations:
            evidence[passage.id] = passage
        source_ids.update(outcome.outcome.source_ids)
        for memory in outcome.outcome.memories:
            memories[memory.id] = memory
        if not outcome.step_finalized:
            await self.journal.finish_step(
                outcome.step.id,
                status=(
                    RunStepStatus.COMPLETED
                    if outcome.ok
                    else RunStepStatus.CANCELLED
                    if outcome.outcome.status is ToolOutcomeStatus.CANCELLED
                    else RunStepStatus.FAILED
                ),
                checkpoint={"messages": messages} if outcome.ok else None,
                error=None if outcome.ok else "tool call failed",
            )
        await emit(
            AgentEventKind.TOOL_CANCELLED
            if outcome.outcome.status is ToolOutcomeStatus.CANCELLED
            else AgentEventKind.TOOL_COMPLETED,
            {
                "name": call.name,
                "tool_call_id": call.id,
                "ok": outcome.ok,
                "duration_ms": outcome.duration_ms,
                **outcome.event_data,
                "presentation": tool_presentation(
                    call.name,
                    outcome.arguments,
                    outcome="success" if outcome.ok else "failed",
                ),
            },
        )
        if governor is not None:
            await emit(
                AgentEventKind.BUDGET_UPDATED,
                {"status": "running", "budget": (await governor.current()).as_dict()},
            )

    def reset_aggregate_budget(self) -> None:
        self._aggregate_used = 0

    async def _enforce_aggregate_budget(self, item: ToolCallOutcome) -> None:
        encoded = item.result_text.encode("utf-8")
        if self._aggregate_used + len(encoded) <= self.aggregate_result_bytes:
            self._aggregate_used += len(encoded)
            return
        context = self.context_factory(str(item.run_id))
        descriptor: dict[str, object]
        delivery = DeliveryStatus.DELIVERY_FAILED
        artifact_id: str | None = None
        if context.artifacts is not None and item.invocation_id is not None:
            try:
                artifact = await context.artifacts.put(
                    session_id=context.session_id,
                    run_id=str(item.run_id),
                    invocation_id=item.invocation_id,
                    content=encoded,
                )
            except Exception as exc:
                descriptor = {
                    "preview": encoded[:4096].decode("utf-8", "replace"),
                    "original_bytes": len(encoded),
                    "warning": f"aggregate artifact delivery failed: {type(exc).__name__}",
                }
            else:
                artifact_id = artifact.id
                delivery = DeliveryStatus.ARTIFACT
                descriptor = {
                    "artifact_id": artifact.id,
                    "sha256": artifact.sha256,
                    "original_bytes": len(encoded),
                    "preview": artifact.preview,
                    "read_with": "read_tool_artifact",
                    "reason": "aggregate_tool_result_budget",
                }
        else:
            descriptor = {
                "preview": encoded[:4096].decode("utf-8", "replace"),
                "original_bytes": len(encoded),
                "warning": "aggregate tool result budget exceeded",
            }
        item.outcome = replace(
            item.outcome,
            data=descriptor,
            content=(ToolContent.artifact(descriptor),) if artifact_id else (),
            delivery_status=delivery,
        )
        item.result_text = item.outcome.for_model()
        item.artifact_id = artifact_id
        self._aggregate_used += len(item.result_text.encode("utf-8"))
        if item.invocation_id is not None and hasattr(
            self.journal, "store_result_replacement"
        ):
            await self.journal.store_result_replacement(
                tool_call_id=item.call.id,
                session_id=context.session_id,
                invocation_id=item.invocation_id,
                delivery_status=delivery.value,
                replacement=json.loads(item.result_text),
            )
        if item.invocation_id is not None and hasattr(
            self.journal, "replace_tool_delivery"
        ):
            await self.journal.replace_tool_delivery(
                item.invocation_id,
                delivery_status=delivery.value,
                result_preview=item.result_text,
                artifact_id=artifact_id,
            )

    async def execute(
        self,
        call: ModelToolCall,
        *,
        messages: list[dict[str, object]],
        run_id: str,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
        governor: RunGovernor | None,
        evidence: dict[str, Evidence],
        source_ids: set[str],
        memories: dict[str, object],
    ) -> None:
        outcome = await self.prepare(
            call,
            messages=messages,
            run_id=run_id,
            emit=emit,
            governor=governor,
        )
        async def commit() -> None:
            await self.commit(
                outcome,
                messages=messages,
                emit=emit,
                governor=governor,
                evidence=evidence,
                source_ids=source_ids,
                memories=memories,
            )

        if outcome.policy.interrupt_behavior.value == "shield":
            task = asyncio.create_task(commit())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                raise
        else:
            await commit()
        if outcome.interrupt_after:
            raise asyncio.CancelledError

    async def execute_batch(
        self,
        calls: tuple[ModelToolCall, ...],
        *,
        messages: list[dict[str, object]],
        run_id: str,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
        governor: RunGovernor | None,
        evidence: dict[str, Evidence],
        source_ids: set[str],
        memories: dict[str, object],
    ) -> None:
        tasks = [
            asyncio.create_task(
                self.prepare(
                    call,
                    messages=messages,
                    run_id=run_id,
                    emit=emit,
                    governor=governor,
                    cancellation_result=True,
                )
            )
            for call in calls
        ]
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                if any(
                    task.result().policy.fail_fast and not task.result().ok
                    for task in done
                ):
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    pending = set()
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in tasks:
            if task.cancelled():
                continue
            await self.commit(
                task.result(),
                messages=messages,
                emit=emit,
                governor=governor,
                evidence=evidence,
                source_ids=source_ids,
                memories=memories,
            )


class ToolLoop:
    def __init__(
        self,
        *,
        chat_model: ChatModel,
        model: str,
        tools: ToolRuntime,
        journal: RunJournal,
        max_tool_rounds: int,
        context_factory: Callable[[str], ExecutionContext],
        max_read_concurrency: int = 4,
        aggregate_result_bytes: int = 65_536,
    ) -> None:
        self.chat_model = chat_model
        self.model = model
        self.tools = tools
        self.journal = journal
        self.max_tool_rounds = max_tool_rounds
        self.context_factory = context_factory
        self.max_read_concurrency = max(1, max_read_concurrency)
        self.model_steps = ModelStepExecutor(journal=journal, model=model, tools=tools)
        self.tool_calls = ToolCallExecutor(
            journal=journal,
            tools=tools,
            context_factory=context_factory,
            aggregate_result_bytes=aggregate_result_bytes,
        )

    async def run(
        self,
        messages: list[dict[str, object]],
        run_id: str,
        *,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
        governor: RunGovernor | None = None,
        authorize_limit: Callable[[BudgetSnapshot], Awaitable[bool]] | None = None,
        chat_model: ChatModel | None = None,
        compact_context: Callable[
            [list[dict[str, object]]], Awaitable[list[dict[str, object]]]
        ]
        | None = None,
    ) -> ToolLoopResult:
        active_model = chat_model or self.chat_model
        evidence, source_ids, memories = {}, set(), {}
        input_tokens = output_tokens = 0
        turn = 0
        while True:
            await self.tools.refresh_dynamic()
            if compact_context is not None:
                messages[:] = await compact_context(messages)
            if governor is not None:
                try:
                    await governor.before_model()
                except RunStopped as stopped:
                    await emit(
                        AgentEventKind.LIMIT_REACHED,
                        {
                            "status": "paused"
                            if stopped.snapshot.mode is RunMode.INTERACTIVE
                            else "stopping",
                            "stop_reason": stopped.reason.value,
                            "budget": stopped.snapshot.as_dict(),
                            "detail": stopped.detail,
                        },
                    )
                    stopped.detail["_emitted"] = True
                    if (
                        stopped.reason is StopReason.MAX_TOOL_ROUNDS
                        and stopped.snapshot.mode is RunMode.INTERACTIVE
                        and authorize_limit is not None
                        and await authorize_limit(stopped.snapshot)
                    ):
                        snapshot = await governor.extend_tool_rounds(32)
                        await emit(
                            AgentEventKind.BUDGET_EXTENDED,
                            {
                                "status": "running",
                                "increment": {"tool_rounds": 32},
                                "budget": snapshot.as_dict(),
                            },
                        )
                        continue
                    if stopped.summarize:
                        return await self._summarize_stop(
                            messages,
                            run_id,
                            emit,
                            stopped,
                            evidence,
                            source_ids,
                            memories,
                            input_tokens,
                            output_tokens,
                            governor,
                            active_model,
                        )
                    raise
            await emit(AgentEventKind.THINKING, {})
            model_step, message, usage = await self.model_steps.invoke(
                chat_model=active_model,
                messages=messages,
                run_id=run_id,
                emit=emit,
                governor=governor,
            )
            input_tokens += usage.input_tokens
            output_tokens += usage.output_tokens
            if governor is not None:
                await governor.record_model_usage(
                    usage.input_tokens, usage.output_tokens
                )
            if not message.tool_calls:
                text = (message.content or "").strip()
                if not text:
                    await self.journal.finish_step(
                        model_step.id,
                        status=RunStepStatus.FAILED,
                        error="model returned an empty answer",
                    )
                    raise ToolLoopError(
                        "model returned an empty answer",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                messages.append({"role": "assistant", "content": message.content})
                await self.journal.finish_step(
                    model_step.id,
                    status=RunStepStatus.COMPLETED,
                    checkpoint={"messages": messages},
                )
                return ToolLoopResult(
                    text,
                    evidence,
                    source_ids,
                    memories,
                    input_tokens,
                    output_tokens,
                    await governor.current() if governor else None,
                )
            if governor is None and turn == self.max_tool_rounds:
                await self.journal.finish_step(
                    model_step.id,
                    status=RunStepStatus.FAILED,
                    error="maximum tool-call rounds exceeded",
                )
                raise ToolLoopError(
                    "agent exceeded the maximum number of tool-call rounds",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            if governor is not None:
                snapshot = await governor.record_round()
                await emit(
                    AgentEventKind.BUDGET_UPDATED,
                    {"status": "running", "budget": snapshot.as_dict()},
                )
            assistant_message: dict[str, object] = {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in message.tool_calls
                ],
            }
            if message.reasoning:
                assistant_message["reasoning_content"] = message.reasoning
            messages.append(assistant_message)
            await self.journal.finish_step(
                model_step.id,
                status=RunStepStatus.COMPLETED,
                checkpoint={"messages": messages},
            )
            self.tool_calls.reset_aggregate_budget()
            try:
                for batch in await self._execution_batches(message.tool_calls, run_id):
                    if len(batch) == 1:
                        await self.tool_calls.execute(
                            batch[0],
                            messages=messages,
                            run_id=run_id,
                            emit=emit,
                            governor=governor,
                            evidence=evidence,
                            source_ids=source_ids,
                            memories=memories,
                        )
                    else:
                        await self.tool_calls.execute_batch(
                            batch,
                            messages=messages,
                            run_id=run_id,
                            emit=emit,
                            governor=governor,
                            evidence=evidence,
                            source_ids=source_ids,
                            memories=memories,
                        )
            except ToolLoopPaused as paused:
                call_ids = [call.id for call in message.tool_calls]
                barrier_index = call_ids.index(paused.tool_call_id)
                for remaining in message.tool_calls[barrier_index + 1 :]:
                    await self._cancel_after_barrier(
                        remaining,
                        messages=messages,
                        run_id=run_id,
                        emit=emit,
                    )
                await self.journal.update_step_checkpoint(
                    paused.step_id, {"messages": messages}
                )
                paused.input_tokens = input_tokens
                paused.output_tokens = output_tokens
                raise
            turn += 1
        raise ToolLoopError(
            "agent exceeded the maximum number of tool-call rounds",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def _cancel_after_barrier(
        self,
        call: ModelToolCall,
        *,
        messages: list[dict[str, object]],
        run_id: str,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
    ) -> None:
        context = self.context_factory(run_id)
        step = await self.journal.create_step(run_id, RunStepKind.TOOL)
        contract = self.tools.contract(call.name)
        invocation_id = await self.journal.start_tool_invocation(
            run_id=run_id,
            session_id=context.session_id,
            tool_call_id=call.id,
            name=call.name,
            spec=contract.metadata() if contract else {"name": call.name},
            capabilities={},
            arguments={},
            status="queued",
        )
        outcome = ToolOutcome(
            ToolOutcomeStatus.CANCELLED,
            False,
            error="cancelled after an interactive tool barrier",
            error_code="interaction_barrier",
        )
        result = outcome.for_model()
        await self.journal.finish_tool_invocation(
            invocation_id,
            status="cancelled",
            execution_status="cancelled",
            delivery_status="inline",
            result_preview=result,
            duration_ms=0,
            error_code="interaction_barrier",
        )
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": result}
        )
        await self.journal.finish_step(
            step.id,
            status=RunStepStatus.CANCELLED,
            error="cancelled after an interactive tool barrier",
        )
        await emit(
            AgentEventKind.TOOL_CANCELLED,
            {
                "name": call.name,
                "tool_call_id": call.id,
                "ok": False,
                "reason": "interaction_barrier",
            },
        )

    async def _execution_batches(
        self, calls: tuple[ModelToolCall, ...], run_id: str
    ) -> list[tuple[ModelToolCall, ...]]:
        batches: list[tuple[ModelToolCall, ...]] = []
        pending: list[ModelToolCall] = []

        def flush() -> None:
            while pending:
                batches.append(tuple(pending[: self.max_read_concurrency]))
                del pending[: self.max_read_concurrency]

        for call in calls:
            try:
                arguments = json.loads(call.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                policy = await self.tools.resolve(
                    call.name, self.context_factory(run_id), arguments
                )
                safe = bool(
                    policy.read_only
                    and policy.concurrency_safe
                    and not policy.context_mutation
                    and not policy.destructive
                    and not policy.external_side_effects
                )
            except (SchemaValidationError, ValueError, json.JSONDecodeError):
                safe = False
            if safe:
                pending.append(call)
            else:
                flush()
                batches.append((call,))
        flush()
        return batches

    async def _summarize_stop(
        self,
        messages: list[dict[str, object]],
        run_id: str,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
        stopped: RunStopped,
        evidence: dict[str, Evidence],
        source_ids: set[str],
        memories: dict[str, object],
        input_tokens: int,
        output_tokens: int,
        governor: RunGovernor,
        chat_model: ChatModel,
    ) -> ToolLoopResult:
        messages.append(
            {
                "role": "system",
                "content": (
                    "The user stopped at the tool-round soft limit. Summarize the "
                    "work completed so far. Do not request or imply any new tool use."
                ),
            }
        )
        content: list[str] = []
        usage = ModelUsage()
        try:
            async for delta in stream_model_response(
                chat_model,
                model=self.model,
                messages=messages,
                tools=[],
            ):
                if delta.content:
                    content.append(delta.content)
                    await emit(AgentEventKind.TEXT_DELTA, {"text": delta.content})
                if delta.usage is not None:
                    usage = delta.usage
        except Exception as exc:
            stopped.detail["summary_error"] = str(exc) or type(exc).__name__
        text = "".join(content).strip()
        snapshot = await governor.record_model_usage(
            usage.input_tokens, usage.output_tokens
        )
        return ToolLoopResult(
            text,
            evidence,
            source_ids,
            memories,
            input_tokens + usage.input_tokens,
            output_tokens + usage.output_tokens,
            snapshot,
            stopped.reason,
        )
