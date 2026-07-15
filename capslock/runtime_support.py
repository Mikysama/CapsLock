"""Context construction and citation resolution for the agent runtime."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from .evidence import Evidence
from .model import ChatModel
from .observability import EventSink
from .session import SessionStore
from .tools import RunContext, ToolRegistry


class ContextBuilder:
    def __init__(self, store: SessionStore, max_messages: int, instructions: str) -> None:
        self.store = store
        self.max_messages = max_messages
        self.instructions = instructions

    def build(self, session_id: str, question: str) -> list[dict[str, object]]:
        summary = ""
        if self.store.message_count(session_id) > self.max_messages:
            summary = self.store.compact_summary(session_id, self.max_messages)
        history = self.store.messages(session_id, self.max_messages)
        system = self.instructions + (f"\nEarlier session summary:\n{summary}" if summary else "")
        return [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]


class CitationResolver:
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def resolve(self, text: str, *, evidence: dict[str, Evidence], source_ids: set[str], session_id: str) -> tuple[str, list[object]]:
        evidence_ids = re.findall(r"\[\[evidence:(ev_[a-f0-9]+)\]\]", text)
        citations: list[object] = [evidence[item] for item in evidence_ids if item in evidence]
        for source_id in re.findall(r"\[\[source:([a-f0-9]+)\]\]", text):
            source = self.store.get_source(source_id, session_id=session_id) if source_id in source_ids else None
            if source is not None:
                citations.append(source)
        if evidence and not citations:
            citations = list(evidence.values())
        cleaned = re.sub(r"\s*\[\[(?:evidence:ev_[a-f0-9]+|source:[a-f0-9]+)\]\]", "", text).strip()
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
    input_tokens: int
    output_tokens: int


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
        input_tokens = output_tokens = 0
        for _ in range(self.max_turns):
            response = self.chat_model.complete(model=self.model, messages=messages, tools=self.tools.schemas)
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            message = response.message
            calls = list(message.tool_calls)
            if not calls:
                text = (message.content or "").strip()
                if not text:
                    raise ToolLoopError("model returned an empty answer", input_tokens=input_tokens, output_tokens=output_tokens)
                return ToolLoopResult(text, evidence, source_ids, input_tokens, output_tokens)
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
                    result_text = result.for_model()
                    self.store.record_tool_call(run_id, call.name, arguments, result.ok, result_text, duration_ms)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})
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

    def start(self, session_id: str, question: str) -> RunState:
        state = RunState(self.store.start_run(session_id, question), time.monotonic(), self.events.mark())
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
