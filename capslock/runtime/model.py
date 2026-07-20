"""Provider-neutral asynchronous chat model protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: str


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


class ChatModel(Protocol):
    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelResponse: ...


class AsyncOpenAIChatModel:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelResponse:
        arguments: dict[str, object] = {"model": model, "messages": messages}
        if tools:
            arguments["tools"] = tools
        response = await self.client.chat.completions.create(**arguments)
        return self._response(response)

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ModelDelta]:
        arguments: dict[str, object] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            arguments["tools"] = tools
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
) -> AsyncIterator[ModelDelta]:
    stream = getattr(chat_model, "stream_complete", None)
    if callable(stream):
        async for delta in stream(model=model, messages=messages, tools=tools):
            yield delta
        return
    response = await chat_model.complete(model=model, messages=messages, tools=tools)
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
