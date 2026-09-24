"""Single asynchronous streaming model/tool loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from ..behavior_defaults import (
    DEFAULT_MAX_ARGUMENT_REPAIR_ATTEMPTS,
    DEFAULT_MAX_READ_CONCURRENCY,
)
from ..domain import (
    AgentEventKind,
    BudgetSnapshot,
    ModelErrorCode,
    ModelRoutingError,
    RunMode,
    RunStepKind,
    RunStepStatus,
    RunStopped,
    StopReason,
)
from ..evidence import Evidence
from ..ports import RunJournal
from ..tooling.contracts import (
    ExecutionContext,
    ResolvedToolPolicy,
    ToolExecutionState,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
)
from ..tooling.executor import ToolRuntime
from ..tooling.presentation import tool_presentation
from ..tooling.schema import SchemaValidationError
from .governance import RunGovernor
from .model import (
    ChatModel,
    ModelMessage,
    ModelToolCall,
    ModelUsage,
    stream_model_response,
)
from .tool_delivery import BatchScheduler, ResultDelivery
from .tool_invocation import InvocationPreparer


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


@dataclass(frozen=True)
class ToolRepairDirective:
    candidates: tuple[str, ...]
    failures: tuple[dict[str, object], ...]

    def prompt(self) -> dict[str, object]:
        return {
            "role": "system",
            "content": (
                "The previous tool invocation was rejected before execution. "
                "Correct it once using only the advertised repair tools. Do not "
                "change user intent, paths, commands, URLs, or business values by "
                "guessing. If a safe correction is impossible, answer without a tool. "
                "Failures: "
                + json.dumps(self.failures, ensure_ascii=False, default=str)
            ),
        }


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
        tool_schemas: list[dict[str, object]] | None = None,
        response_format: dict[str, object] | None = None,
        usage_observer: Callable[
            [list[dict[str, object]], list[dict[str, object]], int], Awaitable[None]
        ]
        | None = None,
    ):
        step = await self.journal.create_step(run_id, RunStepKind.MODEL)
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = ModelUsage()
        try:
            active_schemas = (
                self.tools.schemas if tool_schemas is None else tool_schemas
            )
            stream = stream_model_response(
                chat_model,
                model=self.model,
                messages=messages,
                tools=active_schemas,
                response_format=response_format,
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
            if (
                isinstance(exc, ModelRoutingError)
                and exc.code is ModelErrorCode.CONTEXT_OVERFLOW
                and (content or reasoning or calls)
            ):
                raise ModelRoutingError(
                    "model stream failed after output started; retry suppressed"
                ) from exc
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
        if usage_observer is not None:
            await usage_observer(messages, active_schemas, usage.input_tokens)
        return step, message, usage


class ToolCallExecutor:
    def __init__(
        self,
        *,
        journal: RunJournal,
        tools: ToolRuntime,
        context_factory: Callable[[str], ExecutionContext],
        aggregate_result_bytes: int = 65_536,
        max_argument_repair_attempts: int = DEFAULT_MAX_ARGUMENT_REPAIR_ATTEMPTS,
    ) -> None:
        self.journal = journal
        self.tools = tools
        self.context_factory = context_factory
        self.delivery = ResultDelivery(
            journal=journal,
            context_factory=context_factory,
            aggregate_result_bytes=aggregate_result_bytes,
        )
        self.batch_scheduler = BatchScheduler()
        self.preparer = InvocationPreparer(
            journal=journal,
            tools=tools,
            context_factory=context_factory,
            outcome_factory=ToolCallOutcome,
            paused_error=ToolLoopPaused,
            max_argument_repair_attempts=max_argument_repair_attempts,
        )

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
        return await self.preparer.prepare(
            call,
            messages=messages,
            run_id=run_id,
            emit=emit,
            governor=governor,
            cancellation_result=cancellation_result,
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
        self.delivery.reset()

    async def _enforce_aggregate_budget(self, item: ToolCallOutcome) -> None:
        await self.delivery.enforce(item)

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
    ) -> ToolCallOutcome:
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
        return outcome

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
    ) -> list[ToolCallOutcome]:
        async def prepare(call):
            return await self.prepare(
                call,
                messages=messages,
                run_id=run_id,
                emit=emit,
                governor=governor,
                cancellation_result=True,
            )

        async def commit(outcome):
            await self.commit(
                outcome,
                messages=messages,
                emit=emit,
                governor=governor,
                evidence=evidence,
                source_ids=source_ids,
                memories=memories,
            )

        return await self.batch_scheduler.run(calls, prepare=prepare, commit=commit)


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
        max_read_concurrency: int = DEFAULT_MAX_READ_CONCURRENCY,
        aggregate_result_bytes: int = 65_536,
        max_argument_repair_attempts: int = DEFAULT_MAX_ARGUMENT_REPAIR_ATTEMPTS,
    ) -> None:
        self.chat_model = chat_model
        self.model = model
        self.tools = tools
        self.journal = journal
        self.max_tool_rounds = max_tool_rounds
        self.context_factory = context_factory
        self.max_read_concurrency = max(1, max_read_concurrency)
        self.max_argument_repair_attempts = max(0, min(2, max_argument_repair_attempts))
        self.model_steps = ModelStepExecutor(journal=journal, model=model, tools=tools)
        self.tool_calls = ToolCallExecutor(
            journal=journal,
            tools=tools,
            context_factory=context_factory,
            aggregate_result_bytes=aggregate_result_bytes,
            max_argument_repair_attempts=self.max_argument_repair_attempts,
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
        compact_context: Callable[..., Awaitable[list[dict[str, object]]]]
        | None = None,
        usage_observer: Callable[
            [list[dict[str, object]], list[dict[str, object]], int], Awaitable[None]
        ]
        | None = None,
        response_format: dict[str, object] | None = None,
    ) -> ToolLoopResult:
        active_model = chat_model or self.chat_model
        evidence, source_ids, memories = {}, set(), {}
        input_tokens = output_tokens = 0
        turn = 0
        pending_repair: ToolRepairDirective | None = None
        repair_attempt = 0
        while True:
            await self.tools.refresh_dynamic()
            for diagnostic in self.tools.pop_refresh_diagnostics():
                self.context_factory(run_id).event(
                    "tool_catalog_refresh_failed", **diagnostic
                )
            if compact_context is not None:
                messages[:] = await compact_context(messages)
            planning_active = await self._refresh_plan_attachment(messages, run_id)
            selection_query = self._selection_query(messages)
            selected_schemas, selected_names = self.tools.model_schemas(
                selection_query, planning=planning_active
            )
            active_repair = pending_repair
            active_repair_attempt = (
                repair_attempt + 1 if active_repair is not None else 0
            )
            if active_repair is not None:
                self.tools.discover(active_repair.candidates)
                selected_schemas = self.tools.catalog.schemas_for(
                    active_repair.candidates, planning=planning_active
                )
            self.context_factory(run_id).event(
                "tool_selection_shadow",
                mode=self.tools.selection_mode.value,
                candidates=list(selected_names),
                advertised_count=len(selected_schemas),
                repair=active_repair is not None,
            )
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
            model_messages = (
                [*messages, active_repair.prompt()]
                if active_repair is not None
                else messages
            )
            recovered_overflow = False
            try:
                model_step, message, usage = await self.model_steps.invoke(
                    chat_model=active_model,
                    messages=model_messages,
                    run_id=run_id,
                    emit=emit,
                    governor=governor,
                    tool_schemas=selected_schemas,
                    usage_observer=usage_observer,
                    response_format=response_format,
                )
            except ModelRoutingError as exc:
                if exc.code is not ModelErrorCode.CONTEXT_OVERFLOW:
                    raise
                runtime_context = self.context_factory(run_id)
                runtime_context.event("context_overflow_detected")
                if compact_context is None:
                    runtime_context.event(
                        "context_overflow_recovery_failed",
                        reason="compaction_unavailable",
                    )
                    raise
                runtime_context.event("context_overflow_recovery_started")
                try:
                    messages[:] = await compact_context(messages, force=True)
                except Exception as recovery_error:
                    runtime_context.event(
                        "context_overflow_recovery_failed",
                        reason=str(recovery_error) or type(recovery_error).__name__,
                    )
                    raise exc from recovery_error
                model_messages = (
                    [*messages, active_repair.prompt()]
                    if active_repair is not None
                    else messages
                )
                try:
                    model_step, message, usage = await self.model_steps.invoke(
                        chat_model=active_model,
                        messages=model_messages,
                        run_id=run_id,
                        emit=emit,
                        governor=governor,
                        tool_schemas=selected_schemas,
                        usage_observer=usage_observer,
                        response_format=response_format,
                    )
                except BaseException as retry_error:
                    runtime_context.event(
                        "context_overflow_recovery_failed",
                        reason=str(retry_error) or type(retry_error).__name__,
                    )
                    raise
                recovered_overflow = True
            if recovered_overflow:
                self.context_factory(run_id).event(
                    "context_overflow_recovery_succeeded"
                )
            input_tokens += usage.input_tokens
            output_tokens += usage.output_tokens
            if governor is not None:
                await governor.record_model_usage(
                    usage.input_tokens, usage.output_tokens
                )
            actual_tools = [call.name for call in message.tool_calls]
            self.context_factory(run_id).event(
                "tool_selection_observed",
                mode=self.tools.selection_mode.value,
                candidates=list(selected_names),
                actual=actual_tools,
                recalled=all(name in selected_names for name in actual_tools),
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
            calls = (
                tuple(
                    replace(call, repair_attempt=active_repair_attempt)
                    for call in message.tool_calls
                )
                if active_repair is not None
                else message.tool_calls
            )
            pending_repair = None
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
                    for call in calls
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
            round_outcomes: list[ToolCallOutcome] = []
            try:
                for batch in await self._execution_batches(calls, run_id):
                    if len(batch) == 1:
                        round_outcomes.append(
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
                        )
                    else:
                        round_outcomes.extend(
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
            if self.max_argument_repair_attempts:
                pending_repair = self._repair_directive(round_outcomes)
                repair_attempt = active_repair_attempt if pending_repair else 0
            turn += 1
        raise ToolLoopError(
            "agent exceeded the maximum number of tool-call rounds",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    @staticmethod
    def _selection_query(messages: list[dict[str, object]]) -> str:
        for item in reversed(messages):
            if item.get("role") == "user":
                return str(item.get("content", ""))[-8_192:]
        return ""

    def _repair_directive(
        self, outcomes: list[ToolCallOutcome]
    ) -> ToolRepairDirective | None:
        candidates: list[str] = []
        failures: list[dict[str, object]] = []
        for item in outcomes:
            outcome = item.outcome
            if (
                outcome.error_code
                not in {"invalid_tool_arguments", "invalid_path", "unsupported_tool"}
                or outcome.effective_execution_state
                is not ToolExecutionState.NOT_STARTED
                or not isinstance(outcome.data, dict)
                or outcome.data.get("retryable") is not True
            ):
                continue
            suggested = outcome.data.get("suggested_tools", [])
            if not isinstance(suggested, list):
                continue
            valid = [
                str(name)
                for name in suggested
                if isinstance(name, str) and self.tools.get(name) is not None
            ]
            if not valid:
                continue
            for name in valid:
                if name not in candidates:
                    candidates.append(name)
            failures.append(
                {
                    "tool_call_id": item.call.id,
                    "tool": item.call.name,
                    "error_code": outcome.error_code,
                    "detail": outcome.data,
                }
            )
        if not failures:
            return None
        return ToolRepairDirective(tuple(candidates[:3]), tuple(failures))

    async def _refresh_plan_attachment(
        self, messages: list[dict[str, object]], run_id: str
    ) -> bool:
        prefixes = ("<capslock-plan-mode>", "<capslock-plan-context>")
        messages[:] = [
            item
            for item in messages
            if not (
                item.get("role") == "system"
                and str(item.get("content", "")).startswith(prefixes)
            )
        ]
        context = self.context_factory(run_id)
        if context.planning is None:
            return False
        active_attachment = await context.planning.attachment(context.session_id)
        attachment = active_attachment
        if attachment is None:
            attachment = await context.planning.context_attachment(context.session_id)
        if attachment is None:
            return False
        insertion = 1 if messages and messages[0].get("role") == "system" else 0
        messages.insert(insertion, {"role": "system", "content": attachment})
        return active_attachment is not None

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
        messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
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
                safe = policy.concurrency_safe
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
