"""Provider-neutral synchronous chat model interface."""

from __future__ import annotations

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


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ModelResponse:
    message: ModelMessage
    usage: ModelUsage = ModelUsage()


class ChatModel(Protocol):
    def complete(self, *, model: str, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> ModelResponse: ...


class OpenAIChatModel:
    """Translate an OpenAI-compatible SDK response into internal types."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def complete(self, *, model: str, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> ModelResponse:
        response = self.client.chat.completions.create(model=model, messages=messages, tools=tools)
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
