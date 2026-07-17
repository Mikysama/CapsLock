"""Provider-neutral synchronous chat model interface."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelMessage:
    content: str | None
    tool_calls: tuple[ModelToolCall, ...] = ()


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
    tool_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: str = ""
    usage: ModelUsage | None = None


class ChatModel(Protocol):
    def complete(self, *, model: str, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> ModelResponse: ...


class OpenAIChatModel:
    """Translate an OpenAI-compatible SDK response into internal types."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def complete(self, *, model: str, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> ModelResponse:
        arguments: dict[str, object] = {"model": model, "messages": messages}
        if tools:
            arguments["tools"] = tools
        response = self.client.chat.completions.create(**arguments)
        return self._response(response)

    @staticmethod
    def _response(response: Any) -> ModelResponse:
        raw_message = response.choices[0].message
        calls = tuple(
            ModelToolCall(call.id, call.function.name, call.function.arguments)
            for call in (raw_message.tool_calls or ())
        )
        usage = getattr(response, "usage", None)
        return ModelResponse(
            ModelMessage(raw_message.content, calls),
            ModelUsage(
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
            ),
        )

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ModelDelta]:
        arguments: dict[str, object] = {"model": model, "messages": messages, "stream": True}
        if tools:
            arguments["tools"] = tools
        # OpenAI-compatible providers do not all accept stream_options.
        stream = await asyncio.to_thread(self.client.chat.completions.create, **arguments)
        if not hasattr(stream, "__next__"):
            response = self._response(stream)
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
        sentinel = object()

        def next_chunk() -> object:
            try:
                return next(stream)
            except StopIteration:
                return sentinel

        while True:
            chunk = await asyncio.to_thread(next_chunk)
            if chunk is sentinel:
                break
            choices = getattr(chunk, "choices", ()) or ()
            if choices:
                raw = getattr(choices[0], "delta", None)
                if raw is not None:
                    content = getattr(raw, "content", None)
                    if content:
                        yield ModelDelta(content=str(content))
                    for call in getattr(raw, "tool_calls", ()) or ():
                        function = getattr(call, "function", None)
                        yield ModelDelta(
                            tool_index=int(getattr(call, "index", 0) or 0),
                            tool_call_id=getattr(call, "id", None),
                            tool_name=getattr(function, "name", None) if function else None,
                            tool_arguments=str(getattr(function, "arguments", "") or "") if function else "",
                        )
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                yield ModelDelta(
                    usage=ModelUsage(
                        int(getattr(usage, "prompt_tokens", 0) or 0),
                        int(getattr(usage, "completion_tokens", 0) or 0),
                    )
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
    response = await asyncio.to_thread(
        chat_model.complete,
        model=model,
        messages=messages,
        tools=tools,
    )
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
