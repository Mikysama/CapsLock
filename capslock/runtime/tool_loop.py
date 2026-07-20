"""Single asynchronous streaming model/tool loop."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..domain import AgentEventKind, RunStepKind, RunStepStatus
from ..evidence import Evidence
from ..storage.repositories_v2 import WorkspaceRepositories
from ..tooling.async_core import RunContext, ToolRegistry
from .model import (
    ChatModel,
    ModelMessage,
    ModelToolCall,
    ModelUsage,
    stream_model_response,
)


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


class ToolLoop:
    def __init__(
        self,
        *,
        chat_model: ChatModel,
        model: str,
        tools: ToolRegistry,
        repositories: WorkspaceRepositories,
        max_turns: int,
        context_factory: Callable[[str], RunContext],
    ) -> None:
        self.chat_model = chat_model
        self.model = model
        self.tools = tools
        self.repositories = repositories
        self.max_turns = max_turns
        self.context_factory = context_factory

    async def run(
        self,
        messages: list[dict[str, object]],
        run_id: str,
        *,
        emit: Callable[[AgentEventKind, dict[str, Any]], Awaitable[None]],
    ) -> ToolLoopResult:
        evidence, source_ids, memories = {}, set(), {}
        input_tokens = output_tokens = 0
        for turn in range(self.max_turns + 1):
            await emit(AgentEventKind.THINKING, {})
            model_step = await self.repositories.workflow.create_step(
                run_id, RunStepKind.MODEL
            )
            content: list[str] = []
            reasoning: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            usage = ModelUsage()
            try:
                async for delta in stream_model_response(
                    self.chat_model,
                    model=self.model,
                    messages=messages,
                    tools=self.tools.schemas,
                ):
                    if delta.reasoning:
                        reasoning.append(delta.reasoning)
                        await emit(AgentEventKind.THINKING, {"text": delta.reasoning})
                    if delta.content:
                        content.append(delta.content)
                        await emit(AgentEventKind.TEXT_DELTA, {"text": delta.content})
                    if delta.tool_index is not None:
                        call = calls.setdefault(
                            delta.tool_index, {"id": "", "name": "", "arguments": ""}
                        )
                        if delta.tool_call_id:
                            call["id"] = delta.tool_call_id
                        if delta.tool_name:
                            call["name"] += delta.tool_name
                        call["arguments"] += delta.tool_arguments
                    if delta.usage is not None:
                        usage = delta.usage
            except Exception as exc:
                await self.repositories.workflow.finish_step(
                    model_step.id, status=RunStepStatus.FAILED, error=str(exc)
                )
                raise
            input_tokens += usage.input_tokens
            output_tokens += usage.output_tokens
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
            if not message.tool_calls:
                text = (message.content or "").strip()
                if not text:
                    await self.repositories.workflow.finish_step(
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
                await self.repositories.workflow.finish_step(
                    model_step.id,
                    status=RunStepStatus.COMPLETED,
                    checkpoint={"messages": messages},
                )
                return ToolLoopResult(
                    text, evidence, source_ids, memories, input_tokens, output_tokens
                )
            if turn == self.max_turns:
                await self.repositories.workflow.finish_step(
                    model_step.id,
                    status=RunStepStatus.FAILED,
                    error="maximum tool-call rounds exceeded",
                )
                raise ToolLoopError(
                    "agent exceeded the maximum number of tool-call rounds",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
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
            await self.repositories.workflow.finish_step(
                model_step.id,
                status=RunStepStatus.COMPLETED,
                checkpoint={"messages": messages},
            )
            for call in message.tool_calls:
                step = await self.repositories.workflow.create_step(
                    run_id, RunStepKind.TOOL
                )
                await emit(
                    AgentEventKind.TOOL_RUNNING,
                    {"name": call.name, "tool_call_id": call.id},
                )
                arguments, result_text, duration_ms, ok = {}, "", 0, False
                try:
                    arguments = json.loads(call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                except (json.JSONDecodeError, ValueError) as exc:
                    result_text = json.dumps(
                        {"ok": False, "error": f"invalid tool arguments: {exc}"}
                    )
                    await self.repositories.workflow.record_tool_call(
                        run_id, call.name, {}, False, result_text, 0
                    )
                else:
                    result, duration_ms = await self.tools.invoke(
                        call.name, self.context_factory(run_id), arguments
                    )
                    for passage in result.citations:
                        evidence[passage.id] = passage
                    source_ids.update(result.source_ids)
                    for memory in result.memories:
                        memories[memory.id] = memory
                    result_text, ok = result.for_model(), result.ok
                    audit_arguments = (
                        arguments
                        if result.audit_arguments is None
                        else result.audit_arguments
                    )
                    await self.repositories.workflow.record_tool_call(
                        run_id,
                        call.name,
                        audit_arguments,
                        ok,
                        result.for_audit(),
                        duration_ms,
                    )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result_text}
                )
                await self.repositories.workflow.finish_step(
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
        raise ToolLoopError(
            "agent exceeded the maximum number of tool-call rounds",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
