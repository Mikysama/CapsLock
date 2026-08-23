"""Provider-neutral asynchronous chat model protocol."""

from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..domain import ModelRole, RunLimits


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: str
    repair_attempt: int = 0


@dataclass(frozen=True)
class ModelMessage:
    content: str | None
    tool_calls: tuple[ModelToolCall, ...] = ()
    reasoning: str | None = None


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ModelResponse:
    message: ModelMessage
    usage: ModelUsage = ModelUsage()


@dataclass(frozen=True)
class ModelDelta:
    content: str = ""
    reasoning: str = ""
    tool_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: str = ""
    usage: ModelUsage | None = None


@dataclass(frozen=True)
class ModelRunContext:
    run_id: str
    role: ModelRole = ModelRole.REASONING
    limits: RunLimits | None = None
    budget_base: tuple[int, float] = (0, 0.0)
    hard_budget: bool = False


class ChatModel(Protocol):
    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
    ) -> ModelResponse: ...


@runtime_checkable
class StreamingChatModel(Protocol):
    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ModelDelta]: ...


@runtime_checkable
class ModelSessionProvider(Protocol):
    def open_session(self, context: ModelRunContext) -> "ModelRunSession": ...


class ModelRunSession:
    """Explicit per-run model surface used by the runtime."""

    metered = False

    def __init__(self, model: ChatModel, context: ModelRunContext) -> None:
        self.model = model
        self.context = context

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        arguments: dict[str, object] = {
            "model": model,
            "messages": messages,
            "tools": tools,
        }
        if max_output_tokens is not None:
            arguments["max_output_tokens"] = max_output_tokens
        return await self.model.complete(**arguments)

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ModelDelta]:
        async for delta in stream_model_response(
            self.model,
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
        ):
            yield delta

    def for_role(self, role: ModelRole) -> "ModelRunSession":
        return ModelRunSession(
            self.model,
            ModelRunContext(
                self.context.run_id,
                role,
                self.context.limits,
                self.context.budget_base,
                self.context.hard_budget,
            ),
        )

    async def summary(self) -> list[dict[str, Any]]:
        return []


def open_model_session(model: ChatModel, context: ModelRunContext) -> ModelRunSession:
    if isinstance(model, ModelSessionProvider):
        return model.open_session(context)
    return ModelRunSession(model, context)


class AsyncOpenAIChatModel:
    def __init__(
        self,
        client: Any,
        *,
        max_output_tokens: dict[str, int] | None = None,
        strict_tools: bool = False,
    ) -> None:
        self.client = client
        self.max_output_tokens = max_output_tokens or {}
        self.strict_tools = strict_tools

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        arguments: dict[str, object] = {
            "model": model,
            "messages": _openai_messages(messages),
        }
        if tools:
            arguments["tools"] = _strict_tools(tools) if self.strict_tools else tools
        configured = self.max_output_tokens.get(model)
        effective = (
            min(configured, max_output_tokens)
            if configured is not None and max_output_tokens is not None
            else configured or max_output_tokens
        )
        if effective is not None:
            arguments["max_tokens"] = effective
        response = await self.client.chat.completions.create(**arguments)
        return self._response(response)

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ModelDelta]:
        arguments: dict[str, object] = {
            "model": model,
            "messages": _openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            arguments["tools"] = _strict_tools(tools) if self.strict_tools else tools
        configured = self.max_output_tokens.get(model)
        effective = (
            min(configured, max_output_tokens)
            if configured is not None and max_output_tokens is not None
            else configured or max_output_tokens
        )
        if effective is not None:
            arguments["max_tokens"] = effective
        stream = await self.client.chat.completions.create(**arguments)
        if not hasattr(stream, "__aiter__"):
            response = self._response(stream)
            if response.message.reasoning:
                yield ModelDelta(reasoning=response.message.reasoning)
            if response.message.content:
                yield ModelDelta(content=response.message.content)
            for index, call in enumerate(response.message.tool_calls):
                yield ModelDelta(
                    tool_index=index,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    tool_arguments=call.arguments,
                )
            yield ModelDelta(usage=response.usage)
            return
        async for chunk in stream:
            choices = getattr(chunk, "choices", ()) or ()
            if choices:
                raw = getattr(choices[0], "delta", None)
                if raw is not None:
                    reasoning = getattr(raw, "reasoning_content", None) or getattr(
                        raw, "reasoning", None
                    )
                    if reasoning:
                        yield ModelDelta(reasoning=str(reasoning))
                    content = getattr(raw, "content", None)
                    if content:
                        yield ModelDelta(content=str(content))
                    for call in getattr(raw, "tool_calls", ()) or ():
                        function = getattr(call, "function", None)
                        yield ModelDelta(
                            tool_index=int(getattr(call, "index", 0) or 0),
                            tool_call_id=getattr(call, "id", None),
                            tool_name=getattr(function, "name", None)
                            if function
                            else None,
                            tool_arguments=str(getattr(function, "arguments", "") or "")
                            if function
                            else "",
                        )
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                yield ModelDelta(usage=_usage(usage))

    @staticmethod
    def _response(response: Any) -> ModelResponse:
        raw = response.choices[0].message
        calls = tuple(
            ModelToolCall(call.id, call.function.name, call.function.arguments)
            for call in (raw.tool_calls or ())
        )
        return ModelResponse(
            ModelMessage(
                raw.content,
                calls,
                getattr(raw, "reasoning_content", None)
                or getattr(raw, "reasoning", None),
            ),
            _usage(getattr(response, "usage", None)),
        )


async def stream_model_response(
    chat_model: ChatModel,
    *,
    model: str,
    messages: list[dict[str, object]],
    tools: list[dict[str, object]],
    max_output_tokens: int | None = None,
) -> AsyncIterator[ModelDelta]:
    if isinstance(chat_model, StreamingChatModel):
        arguments: dict[str, object] = {
            "model": model,
            "messages": messages,
            "tools": tools,
        }
        if max_output_tokens is not None:
            arguments["max_output_tokens"] = max_output_tokens
        async for delta in chat_model.stream_complete(**arguments):
            yield delta
        return
    arguments = {"model": model, "messages": messages, "tools": tools}
    if max_output_tokens is not None:
        arguments["max_output_tokens"] = max_output_tokens
    response = await chat_model.complete(**arguments)
    if response.message.reasoning:
        yield ModelDelta(reasoning=response.message.reasoning)
    if response.message.content:
        yield ModelDelta(content=response.message.content)
    for index, call in enumerate(response.message.tool_calls):
        yield ModelDelta(
            tool_index=index,
            tool_call_id=call.id,
            tool_name=call.name,
            tool_arguments=call.arguments,
        )
    yield ModelDelta(usage=response.usage)


def _usage(raw: Any) -> ModelUsage:
    return ModelUsage(
        int(getattr(raw, "prompt_tokens", 0) or 0),
        int(getattr(raw, "completion_tokens", 0) or 0),
    )


def _strict_tools(tools: list[dict[str, object]]) -> list[dict[str, object]]:
    output = deepcopy(tools)
    for item in output:
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            _strict_schema(parameters)
        function["strict"] = True
    return output


def _strict_schema(schema: dict[str, object]) -> None:
    raw_type = schema.get("type")
    if raw_type == "object":
        schema["additionalProperties"] = False
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            originally_required = set(schema.get("required", ()))
            schema["required"] = list(properties)
            for name, child in properties.items():
                if not isinstance(child, dict):
                    continue
                _strict_schema(child)
                if name not in originally_required:
                    _make_nullable(child)
    items = schema.get("items")
    if isinstance(items, dict):
        _strict_schema(items)
    for keyword in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for child in variants:
                if isinstance(child, dict):
                    _strict_schema(child)


def _make_nullable(schema: dict[str, object]) -> None:
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        schema["type"] = [raw_type, "null"]
    elif isinstance(raw_type, list) and "null" not in raw_type:
        schema["type"] = [*raw_type, "null"]
    elif "anyOf" in schema and isinstance(schema["anyOf"], list):
        schema["anyOf"] = [*schema["anyOf"], {"type": "null"}]


def _openai_messages(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Map provider-neutral rich tool blocks to OpenAI-compatible content parts."""
    output: list[dict[str, object]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            output.append(message)
            continue
        parts: list[dict[str, object]] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append({"type": "text", "text": str(block)})
                continue
            kind, value = block.get("type"), block.get("value")
            if kind == "text":
                parts.append({"type": "text", "text": str(value or "")})
            elif kind == "image":
                if isinstance(value, str):
                    url, detail = value, None
                elif isinstance(value, dict):
                    url = value.get("url", value.get("image_url", value.get("data")))
                    detail = value.get("detail")
                else:
                    url, detail = None, None
                if isinstance(url, str):
                    image_url: dict[str, object] = {"url": url}
                    if detail in {"auto", "low", "high"}:
                        image_url["detail"] = detail
                    parts.append({"type": "image_url", "image_url": image_url})
                else:
                    parts.append(
                        {
                            "type": "text",
                            "text": json.dumps(value, ensure_ascii=False, default=str),
                        }
                    )
            else:
                parts.append(
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"type": kind, "value": value},
                            ensure_ascii=False,
                            default=str,
                        ),
                    }
                )
        output.append({**message, "content": parts})
    return output
