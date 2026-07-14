"""The extensible tool-calling runtime used by the workspace CLI."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import Evidence
from .observability import EventSink
from .policy import WorkspacePolicy
from .session import SessionStore
from .tools import RunContext, ToolRegistry, workspace_tools


class AgentRuntimeError(RuntimeError):
    pass


INSTRUCTIONS = """You are CapsLock, a trustworthy workspace assistant.
Use workspace tools for claims about local files or Git. For edits, first call a propose_file_* tool:
it only creates a reviewable proposal and never writes a user file. Never call apply_change unless
the user has explicitly approved the proposal in the CLI. For tests or checks, only call
propose_command with a fixed template; never call run_command unless the user has explicitly approved
the proposal in the CLI. Never claim to have run arbitrary commands, used the network, or accessed a
path outside the workspace. Tool failures are recoverable: explain the limit or try a valid alternative.
Cite evidence returned by tools with [[evidence:ev_xxx]] markers.
If local evidence is insufficient, say so plainly. Keep answers concise."""


@dataclass(frozen=True)
class WorkspaceAnswer:
    text: str
    citations: list[Evidence]
    events: list[object]
    session_id: str
    run_id: str
    duration_ms: int


class WorkspaceAgent:
    def __init__(
        self,
        client: Any,
        *,
        workspace: str | Path,
        model: str,
        store: SessionStore,
        session_id: str | None = None,
        tools: ToolRegistry | None = None,
        max_turns: int = 6,
        max_context_messages: int = 24,
        command_timeout_seconds: float = 120,
        command_output_bytes: int = 100_000,
        input_cost_per_million: float = 0,
        output_cost_per_million: float = 0,
        event_sink: EventSink | None = None,
    ) -> None:
        self.client, self.workspace, self.model = client, Path(workspace).resolve(), model
        self.store, self.tools = store, tools or workspace_tools()
        self.max_turns, self.max_context_messages = max_turns, max_context_messages
        self.command_timeout_seconds, self.command_output_bytes = command_timeout_seconds, command_output_bytes
        self.input_cost_per_million, self.output_cost_per_million = input_cost_per_million, output_cost_per_million
        self.events = event_sink or EventSink(self.workspace / ".capslock" / "events.jsonl")
        existing = store.get(session_id) if session_id else None
        if session_id and existing is None:
            raise AgentRuntimeError(f"session does not exist: {session_id}")
        if existing and existing.workspace.resolve() != self.workspace:
            raise AgentRuntimeError("session belongs to a different workspace")
        self.session_id = session_id or store.create(self.workspace, model).id

    def ask(self, question: str) -> WorkspaceAnswer:
        if not question.strip():
            raise AgentRuntimeError("question must not be empty")
        started = time.monotonic()
        run_id = self.store.start_run(self.session_id, question)
        evidence: dict[str, Evidence] = {}
        input_tokens = output_tokens = 0
        try:
            if self.store.message_count(self.session_id) > self.max_context_messages:
                summary = self.store.compact_summary(self.session_id, self.max_context_messages)
            else:
                summary = ""
            history = self.store.messages(self.session_id, self.max_context_messages)
            system = INSTRUCTIONS + (f"\nEarlier session summary:\n{summary}" if summary else "")
            messages: list[dict[str, object]] = [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]
            self.events.emit("run_started", run_id=run_id, session_id=self.session_id)
            for _ in range(self.max_turns):
                response = self.client.chat.completions.create(model=self.model, messages=messages, tools=self.tools.schemas)
                usage = getattr(response, "usage", None)
                input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
                message = response.choices[0].message
                calls = list(message.tool_calls or [])
                if not calls:
                    text = (message.content or "").strip()
                    if not text:
                        raise AgentRuntimeError("model returned an empty answer")
                    answer = self._answer(text, evidence, run_id, started)
                    self.store.append_message(self.session_id, "user", question)
                    self.store.append_message(self.session_id, "assistant", answer.text)
                    self.store.record_citations(run_id, answer.citations)
                    self.store.finish_run(run_id, status="completed", duration_ms=answer.duration_ms, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=self._cost(input_tokens, output_tokens))
                    self.events.emit("run_finished", run_id=run_id, status="completed", duration_ms=answer.duration_ms)
                    return answer
                messages.append({"role": "assistant", "content": message.content, "tool_calls": [{"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}} for call in calls]})
                for call in calls:
                    try:
                        arguments = json.loads(call.function.arguments)
                        if not isinstance(arguments, dict):
                            raise ValueError("tool arguments must be a JSON object")
                    except (json.JSONDecodeError, ValueError) as exc:
                        arguments, result_text, duration_ms = {}, json.dumps({"ok": False, "error": f"invalid tool arguments: {exc}"}), 0
                        self.store.record_tool_call(run_id, call.function.name, arguments, False, result_text, duration_ms)
                    else:
                        context = RunContext(self.session_id, run_id, WorkspacePolicy(self.workspace), self.max_turns, self.events.emit, self.store, self.command_timeout_seconds, self.command_output_bytes)
                        result, duration_ms = self.tools.invoke(call.function.name, context, arguments)
                        for passage in result.citations:
                            evidence[passage.id] = passage
                        result_text = result.for_model()
                        self.store.record_tool_call(run_id, call.function.name, arguments, result.ok, result_text, duration_ms)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})
            raise AgentRuntimeError("agent exceeded the maximum number of tool-call rounds")
        except KeyboardInterrupt:
            duration_ms = round((time.monotonic() - started) * 1000)
            self.store.finish_run(run_id, status="cancelled", duration_ms=duration_ms, error="cancelled by user", input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=self._cost(input_tokens, output_tokens))
            self.events.emit("run_finished", run_id=run_id, status="cancelled", duration_ms=duration_ms)
            raise
        except Exception as exc:
            duration_ms = round((time.monotonic() - started) * 1000)
            self.store.finish_run(run_id, status="failed", duration_ms=duration_ms, error=str(exc), input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=self._cost(input_tokens, output_tokens))
            self.events.emit("run_finished", run_id=run_id, status="failed", duration_ms=duration_ms)
            raise

    def _answer(self, text: str, evidence: dict[str, Evidence], run_id: str, started: float) -> WorkspaceAnswer:
        ids = re.findall(r"\[\[evidence:(ev_[a-f0-9]+)\]\]", text)
        citations = [evidence[item] for item in ids if item in evidence]
        if evidence and not citations:
            citations = list(evidence.values())
        cleaned = re.sub(r"\s*\[\[evidence:ev_[a-f0-9]+\]\]", "", text).strip()
        return WorkspaceAnswer(cleaned, _unique(citations), list(self.events.events), self.session_id, run_id, round((time.monotonic() - started) * 1000))

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_cost_per_million + output_tokens * self.output_cost_per_million) / 1_000_000


def _unique(passages: list[Evidence]) -> list[Evidence]:
    seen: set[str] = set()
    return [item for item in passages if not (item.id in seen or seen.add(item.id))]
