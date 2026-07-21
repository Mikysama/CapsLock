"""Single asynchronous streaming model/tool loop."""

from __future__ import annotations

import json
import asyncio
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
from ..tooling.async_core import RunContext, ToolRegistry
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


class ModelStepExecutor:
    def __init__(self, *, journal: RunJournal, model: str, tools: ToolRegistry) -> None:
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
        tools: ToolRegistry,
        context_factory: Callable[[str], RunContext],
    ) -> None:
        self.journal = journal
        self.tools = tools
        self.context_factory = context_factory

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
        step = await self.journal.create_step(run_id, RunStepKind.TOOL)
        await emit(
            AgentEventKind.TOOL_RUNNING,
            {"name": call.name, "tool_call_id": call.id},
        )
        arguments, result_text, duration_ms, ok = {}, "", 0, False
        attempt_id: int | None = None
        try:
            arguments = json.loads(call.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            if governor is not None:
                attempt_id, _, _ = await governor.before_tool(
                    call.name, {"invalid_arguments": call.arguments}
                )
            result_text = json.dumps(
                {"ok": False, "error": f"invalid tool arguments: {exc}"}
            )
            await self.journal.record_tool_call(
                run_id, call.name, {}, False, result_text, 0
            )
        else:
            if governor is not None:
                attempt_id, _, _ = await governor.before_tool(call.name, arguments)
            invocation = self.tools.invoke(
                call.name, self.context_factory(run_id), arguments
            )
            timeout = governor.remaining_seconds() if governor else None
            try:
                async with asyncio.timeout(timeout):
                    result, duration_ms = await invocation
            except TimeoutError:
                if governor is not None:
                    await governor.stop(StopReason.MAX_DURATION)
                raise
            for passage in result.citations:
                evidence[passage.id] = passage
            source_ids.update(result.source_ids)
            for memory in result.memories:
                memories[memory.id] = memory
            result_text, ok = result.for_model(), result.ok
            audit_arguments = (
                arguments if result.audit_arguments is None else result.audit_arguments
            )
            await self.journal.record_tool_call(
                run_id,
                call.name,
                audit_arguments,
                ok,
                result.for_audit(),
                duration_ms,
            )
        if governor is not None and attempt_id is not None:
            await governor.finish_tool(attempt_id, ok=ok, duration_ms=duration_ms)
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": result_text}
        )
        await self.journal.finish_step(
            step.id,
            status=RunStepStatus.COMPLETED if ok else RunStepStatus.FAILED,
            checkpoint={"messages": messages} if ok else None,
            error=None if ok else "tool call failed",
        )
        await emit(
            AgentEventKind.TOOL_COMPLETED,
            {
                "name": call.name,
                "tool_call_id": call.id,
                "ok": ok,
                "duration_ms": duration_ms,
            },
        )
        if governor is not None:
            await emit(
                AgentEventKind.BUDGET_UPDATED,
                {"status": "running", "budget": (await governor.current()).as_dict()},
            )


class ToolLoop:
    def __init__(
        self,
        *,
        chat_model: ChatModel,
        model: str,
        tools: ToolRegistry,
        journal: RunJournal,
        max_tool_rounds: int,
        context_factory: Callable[[str], RunContext],
    ) -> None:
        self.chat_model = chat_model
        self.model = model
        self.tools = tools
        self.journal = journal
        self.max_tool_rounds = max_tool_rounds
        self.context_factory = context_factory
        self.model_steps = ModelStepExecutor(journal=journal, model=model, tools=tools)
        self.tool_calls = ToolCallExecutor(
            journal=journal, tools=tools, context_factory=context_factory
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
    ) -> ToolLoopResult:
        active_model = chat_model or self.chat_model
        evidence, source_ids, memories = {}, set(), {}
        input_tokens = output_tokens = 0
        turn = 0
        while True:
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
            for call in message.tool_calls:
                await self.tool_calls.execute(
                    call,
                    messages=messages,
                    run_id=run_id,
                    emit=emit,
                    governor=governor,
                    evidence=evidence,
                    source_ids=source_ids,
                    memories=memories,
                )
            turn += 1
        raise ToolLoopError(
            "agent exceeded the maximum number of tool-call rounds",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

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
