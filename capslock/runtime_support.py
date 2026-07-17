"""Context construction and citation resolution for the agent runtime."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, replace
from collections.abc import Awaitable, Callable
from typing import Any

from .evidence import Evidence
from .domain import AgentEventKind, RunStepKind, RunStepStatus
from .model import ChatModel, ModelMessage, ModelToolCall, ModelUsage, stream_model_response
from .observability import EventSink
from .permissions import PermissionMode
from .session import SessionStore
from .tools import RunContext, ToolRegistry
from .memory import MemoryService


class ContextBuilder:
    def __init__(self, store: SessionStore, max_messages: int, instructions: str, memory: MemoryService | None = None) -> None:
        self.store = store
        self.max_messages = max_messages
        self.instructions = instructions
        self.memory = memory
        self.last_recalls = []

    def build(self, session_id: str, question: str, *, run_id: str | None = None) -> list[dict[str, object]]:
        summary = ""
        excluded = self.memory.excluded_runs() if self.memory is not None else set()
        if self.store.message_count(session_id, excluded_run_ids=excluded) > self.max_messages:
            summary = self.store.compact_summary(session_id, self.max_messages, excluded_run_ids=excluded)
        history = self.store.messages(session_id, self.max_messages, excluded_run_ids=excluded)
        memory_context = ""
        self.last_recalls = []
        if self.memory is not None and run_id is not None:
            memory_context, self.last_recalls = self.memory.recall_context(question, run_id=run_id)
        system = self.instructions + (f"\nEarlier session summary:\n{summary}" if summary else "")
        if memory_context:
            system += "\n\n" + memory_context
        return [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]


class CitationResolver:
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def resolve(self, text: str, *, evidence: dict[str, Evidence], source_ids: set[str], memories: dict[str, object], session_id: str) -> tuple[str, list[object]]:
        evidence_ids = re.findall(r"\[\[evidence:(ev_[a-f0-9]+)\]\]", text)
        citations: list[object] = [evidence[item] for item in evidence_ids if item in evidence]
        for source_id in re.findall(r"\[\[source:([a-f0-9]+)\]\]", text):
            source = self.store.get_source(source_id, session_id=session_id) if source_id in source_ids else None
            if source is not None:
                citations.append(source)
        for memory_id in re.findall(r"\[\[memory:(mem_[a-f0-9]+)\]\]", text):
            if memory_id in memories:
                citations.append(memories[memory_id])
        if evidence and not citations:
            citations = list(evidence.values())
        cleaned = re.sub(r"\s*\[\[(?:evidence:ev_[a-f0-9]+|source:[a-f0-9]+|memory:mem_[a-f0-9]+)\]\]", "", text).strip()
        return cleaned, _unique(citations)


class ToolLoopError(RuntimeError):
    def __init__(self, message: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
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


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError


class ToolLoop:
    def __init__(self, *, chat_model: ChatModel, model: str, tools: ToolRegistry, store: SessionStore, max_turns: int, context_factory: Callable[[str], RunContext]) -> None:
        self.chat_model = chat_model
        self.model = model
        self.tools = tools
        self.store = store
        self.max_turns = max_turns
        self.context_factory = context_factory

    def run(self, messages: list[dict[str, object]], run_id: str) -> ToolLoopResult:
        evidence: dict[str, Evidence] = {}
        source_ids: set[str] = set()
        memories: dict[str, object] = {}
        input_tokens = output_tokens = 0
        for turn in range(self.max_turns + 1):
            response = self.chat_model.complete(model=self.model, messages=messages, tools=self.tools.schemas)
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            message = response.message
            calls = list(message.tool_calls)
            if not calls:
                text = (message.content or "").strip()
                if not text:
                    raise ToolLoopError("model returned an empty answer", input_tokens=input_tokens, output_tokens=output_tokens)
                return ToolLoopResult(text, evidence, source_ids, memories, input_tokens, output_tokens)
            if turn == self.max_turns:
                raise ToolLoopError("agent exceeded the maximum number of tool-call rounds", input_tokens=input_tokens, output_tokens=output_tokens)
            messages.append({"role": "assistant", "content": message.content, "tool_calls": [{"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}} for call in calls]})
            for call in calls:
                try:
                    arguments = json.loads(call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                except (json.JSONDecodeError, ValueError) as exc:
                    arguments, result_text, duration_ms = {}, json.dumps({"ok": False, "error": f"invalid tool arguments: {exc}"}), 0
                    self.store.record_tool_call(run_id, call.name, arguments, False, result_text, duration_ms)
                else:
                    result, duration_ms = self.tools.invoke(call.name, self.context_factory(run_id), arguments)
                    for passage in result.citations:
                        evidence[passage.id] = passage
                    source_ids.update(result.source_ids)
                    for memory in result.memories:
                        memories[memory.id] = memory
                    result_text = result.for_model()
                    audit_arguments = arguments if result.audit_arguments is None else result.audit_arguments
                    self.store.record_tool_call(run_id, call.name, audit_arguments, result.ok, result.for_audit(), duration_ms)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})
        raise ToolLoopError("agent exceeded the maximum number of tool-call rounds", input_tokens=input_tokens, output_tokens=output_tokens)

    async def run_async(
        self,
        messages: list[dict[str, object]],
        run_id: str,
        *,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
        cancellation: CancellationToken,
    ) -> ToolLoopResult:
        evidence: dict[str, Evidence] = {}
        source_ids: set[str] = set()
        memories: dict[str, object] = {}
        input_tokens = output_tokens = 0
        for turn in range(self.max_turns + 1):
            cancellation.raise_if_cancelled()
            await emit(AgentEventKind.THINKING, {})
            content: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            usage = ModelUsage()
            async for delta in stream_model_response(
                self.chat_model,
                model=self.model,
                messages=messages,
                tools=self.tools.schemas,
            ):
                cancellation.raise_if_cancelled()
                if delta.content:
                    content.append(delta.content)
                    await emit(AgentEventKind.TEXT_DELTA, {"text": delta.content})
                if delta.tool_index is not None:
                    call = calls.setdefault(delta.tool_index, {"id": "", "name": "", "arguments": ""})
                    if delta.tool_call_id:
                        call["id"] = delta.tool_call_id
                    if delta.tool_name:
                        call["name"] += delta.tool_name
                    call["arguments"] += delta.tool_arguments
                if delta.usage is not None:
                    usage = delta.usage
            input_tokens += usage.input_tokens
            output_tokens += usage.output_tokens
            message = ModelMessage(
                "".join(content) or None,
                tuple(
                    ModelToolCall(item["id"] or f"call_{index}", item["name"], item["arguments"])
                    for index, item in sorted(calls.items())
                ),
            )
            if not message.tool_calls:
                text = (message.content or "").strip()
                if not text:
                    raise ToolLoopError("model returned an empty answer", input_tokens=input_tokens, output_tokens=output_tokens)
                step = self.store.create_run_step(run_id, RunStepKind.MODEL)
                messages.append({"role": "assistant", "content": message.content})
                self.store.finish_run_step(
                    step.id,
                    status=RunStepStatus.COMPLETED,
                    checkpoint={"messages": messages},
                )
                return ToolLoopResult(text, evidence, source_ids, memories, input_tokens, output_tokens)
            if turn == self.max_turns:
                raise ToolLoopError("agent exceeded the maximum number of tool-call rounds", input_tokens=input_tokens, output_tokens=output_tokens)
            messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                    for call in message.tool_calls
                ],
            })
            for call in message.tool_calls:
                cancellation.raise_if_cancelled()
                step = self.store.create_run_step(run_id, RunStepKind.TOOL)
                await emit(AgentEventKind.TOOL_RUNNING, {"name": call.name, "tool_call_id": call.id})
                try:
                    arguments = json.loads(call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                except (json.JSONDecodeError, ValueError) as exc:
                    arguments = {}
                    result_text = json.dumps({"ok": False, "error": f"invalid tool arguments: {exc}"})
                    duration_ms = 0
                    self.store.record_tool_call(run_id, call.name, arguments, False, result_text, duration_ms)
                    ok = False
                else:
                    run_context = self.context_factory(run_id)
                    result, duration_ms = self.tools.invoke(call.name, run_context, arguments)
                    if (
                        result.ok
                        and call.name in {"propose_mcp_connect", "propose_mcp_call"}
                        and run_context.permission_mode is PermissionMode.FULL_ACCESS
                        and run_context.actions is not None
                        and isinstance(result.data, dict)
                        and isinstance(result.data.get("action_id"), str)
                    ):
                        completed = await run_context.actions.approve_and_execute_async(result.data["action_id"])
                        result = replace(
                            result,
                            data={
                                **result.data,
                                "status": completed.status,
                                "result_kind": completed.result_kind,
                                "result": getattr(completed, "result", None),
                                "error": completed.error,
                            },
                        )
                    for passage in result.citations:
                        evidence[passage.id] = passage
                    source_ids.update(result.source_ids)
                    for memory in result.memories:
                        memories[memory.id] = memory
                    result_text = result.for_model()
                    audit_arguments = arguments if result.audit_arguments is None else result.audit_arguments
                    self.store.record_tool_call(run_id, call.name, audit_arguments, result.ok, result.for_audit(), duration_ms)
                    ok = result.ok
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})
                self.store.finish_run_step(
                    step.id,
                    status=RunStepStatus.COMPLETED if ok else RunStepStatus.FAILED,
                    checkpoint={"messages": messages} if ok else None,
                    error=None if ok else "tool call failed",
                )
                await emit(
                    AgentEventKind.TOOL_COMPLETED,
                    {"name": call.name, "tool_call_id": call.id, "ok": ok, "duration_ms": duration_ms},
                )
        raise ToolLoopError("agent exceeded the maximum number of tool-call rounds", input_tokens=input_tokens, output_tokens=output_tokens)


@dataclass(frozen=True)
class RunState:
    run_id: str
    started: float
    event_mark: int


class RunRecorder:
    def __init__(self, store: SessionStore, events: EventSink, *, input_cost_per_million: float, output_cost_per_million: float) -> None:
        self.store = store
        self.events = events
        self.input_cost = input_cost_per_million
        self.output_cost = output_cost_per_million

    def start(
        self,
        session_id: str,
        question: str,
        *,
        work_item_id: str | None = None,
        parent_run_id: str | None = None,
        resume_from_step_id: str | None = None,
    ) -> RunState:
        state = RunState(
            self.store.start_run(
                session_id,
                question,
                work_item_id=work_item_id,
                parent_run_id=parent_run_id,
                resume_from_step_id=resume_from_step_id,
            ),
            time.monotonic(),
            self.events.mark(),
        )
        self.events.emit("run_started", run_id=state.run_id, session_id=session_id)
        return state

    def finish(self, state: RunState, *, status: str, error: str | None = None, input_tokens: int = 0, output_tokens: int = 0, duration_ms: int | None = None) -> int:
        duration = duration_ms if duration_ms is not None else round((time.monotonic() - state.started) * 1000)
        cost = (input_tokens * self.input_cost + output_tokens * self.output_cost) / 1_000_000
        self.store.finish_run(state.run_id, status=status, duration_ms=duration, error=error, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)
        self.events.emit("run_finished", run_id=state.run_id, status=status, duration_ms=duration)
        return duration


def _unique(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    output: list[Any] = []
    for item in items:
        identifier = str(getattr(item, "id"))
        if identifier not in seen:
            seen.add(identifier)
            output.append(item)
    return output
