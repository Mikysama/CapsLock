"""Provider-neutral asynchronous chat model protocol."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from ..structured_output import strict_provider_schema

from ..domain import ModelErrorCode, ModelRole, RunLimits
from .model_events import (
    protocol_error,
    validate_completion,
    _error_message,
    _optional_string,
    _incomplete_reason,
)


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
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    source: str = field(default="provider", compare=False)
    request_id: str | None = field(default=None, compare=False)
    first_token_ms: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ModelResponse:
    message: ModelMessage
    usage: ModelUsage = ModelUsage(source="unknown")
    completion_status: str | None = None
    incomplete_reason: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ModelDelta:
    content: str = ""
    reasoning: str = ""
    tool_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: str = ""
    usage: ModelUsage | None = None
    completion_status: str | None = None
    incomplete_reason: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ModelRunContext:
    run_id: str
    role: ModelRole = ModelRole.REASONING
    limits: RunLimits | None = None
    budget_base: tuple[int, float] = (0, 0.0)
    hard_budget: bool = False
    deadline_monotonic: float | None = None
    profile_id: str | None = None


class ChatModel(Protocol):
    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
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
        response_format: dict[str, object] | None = None,
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
        response_format: dict[str, object] | None = None,
    ) -> ModelResponse:
        arguments: dict[str, object] = {
            "model": model,
            "messages": messages,
            "tools": tools,
        }
        if max_output_tokens is not None:
            arguments["max_output_tokens"] = max_output_tokens
        if response_format is not None:
            arguments["response_format"] = response_format
        return await self.model.complete(**arguments)

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> AsyncIterator[ModelDelta]:
        async for delta in stream_model_response(
            self.model,
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
            response_format=response_format,
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
                self.context.deadline_monotonic,
                self.context.profile_id,
            ),
        )

    async def summary(self) -> list[dict[str, Any]]:
        return []


def open_model_session(model: ChatModel, context: ModelRunContext) -> ModelRunSession:
    if isinstance(model, ModelSessionProvider):
        return model.open_session(context)
    return ModelRunSession(model, context)


class AsyncOpenAIResponsesModel:
    def __init__(
        self,
        client: Any,
        *,
        strict_tools: bool = True,
    ) -> None:
        self.client = client
        self.strict_tools = strict_tools

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> ModelResponse:
        return await self._complete_responses(
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
            response_format=response_format,
        )

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> AsyncIterator[ModelDelta]:
        async for delta in self._stream_responses(
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
            response_format=response_format,
        ):
            yield delta

    async def _complete_responses(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None,
        response_format: dict[str, object] | None,
    ) -> ModelResponse:
        arguments = self._responses_arguments(
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
            response_format=response_format,
        )
        response = await self.client.responses.create(**arguments)
        return _responses_response(response)

    async def _stream_responses(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None,
        response_format: dict[str, object] | None,
    ) -> AsyncIterator[ModelDelta]:
        arguments = self._responses_arguments(
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
            response_format=response_format,
        )
        arguments["stream"] = True
        stream = await self.client.responses.create(**arguments)
        if not hasattr(stream, "__aiter__"):
            response = _responses_response(stream)
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
            yield response_terminal(response)
            validate_completion(
                response.completion_status,
                response.incomplete_reason,
                response.error_message,
            )
            return
        terminal_seen = False
        headers = getattr(getattr(stream, "response", None), "headers", {}) or {}
        request_id = headers.get("x-request-id") if hasattr(headers, "get") else None
        async for event in stream:
            if terminal_seen:
                raise protocol_error(
                    ModelErrorCode.STREAM_INCOMPLETE,
                    "model stream contains events after its terminal",
                )
            kind = str(getattr(event, "type", ""))
            delta = getattr(event, "delta", None)
            if kind == "response.output_text.delta" and delta:
                yield ModelDelta(content=str(delta))
            elif (
                kind
                in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                }
                and delta
            ):
                yield ModelDelta(reasoning=str(delta))
            elif kind == "response.output_item.added":
                item = getattr(event, "item", None)
                if getattr(item, "type", None) == "function_call":
                    yield ModelDelta(
                        tool_index=int(getattr(event, "output_index", 0) or 0),
                        tool_call_id=str(
                            getattr(item, "call_id", None) or getattr(item, "id", "")
                        ),
                        tool_name=str(getattr(item, "name", "") or ""),
                        tool_arguments=str(getattr(item, "arguments", "") or ""),
                    )
            elif kind == "response.function_call_arguments.delta" and delta:
                yield ModelDelta(
                    tool_index=int(getattr(event, "output_index", 0) or 0),
                    tool_arguments=str(delta),
                )
            elif kind in {
                "response.completed",
                "response.incomplete",
                "response.failed",
            }:
                terminal_seen = True
                response = getattr(event, "response", None)
                status = kind.removeprefix("response.")
                terminal = ModelDelta(
                    usage=replace(
                        _usage(getattr(response, "usage", None)),
                        request_id=_optional_string(
                            getattr(response, "_request_id", None) or request_id
                        ),
                    ),
                    completion_status=status,
                    incomplete_reason=_incomplete_reason(response),
                    error_message=_error_message(getattr(response, "error", None)),
                )
                yield terminal
                validate_completion(
                    status, terminal.incomplete_reason, terminal.error_message
                )
            elif kind == "error":
                yield ModelDelta(
                    completion_status="failed", error_message=_error_message(event)
                )
                raise protocol_error(
                    ModelErrorCode.RESPONSE_FAILED,
                    _error_message(event) or "model response failed",
                )
        if not terminal_seen:
            raise protocol_error(
                ModelErrorCode.STREAM_INCOMPLETE,
                "model stream ended without a terminal event",
            )

    def _responses_arguments(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None,
        response_format: dict[str, object] | None,
    ) -> dict[str, object]:
        arguments: dict[str, object] = {
            "model": model,
            "input": _responses_input(messages),
        }
        if tools:
            arguments["tools"] = _responses_tools(tools, strict=self.strict_tools)
        if max_output_tokens is not None:
            arguments["max_output_tokens"] = max_output_tokens
        if response_format is not None:
            arguments["text"] = {"format": _responses_format(response_format)}
        return arguments


async def stream_model_response(
    chat_model: ChatModel,
    *,
    model: str,
    messages: list[dict[str, object]],
    tools: list[dict[str, object]],
    max_output_tokens: int | None = None,
    response_format: dict[str, object] | None = None,
) -> AsyncIterator[ModelDelta]:
    if isinstance(chat_model, StreamingChatModel):
        arguments: dict[str, object] = {
            "model": model,
            "messages": messages,
            "tools": tools,
        }
        if max_output_tokens is not None:
            arguments["max_output_tokens"] = max_output_tokens
        if response_format is not None:
            arguments["response_format"] = response_format
        terminal_seen = False
        terminal = None
        async for delta in chat_model.stream_complete(**arguments):
            if terminal_seen:
                raise protocol_error(
                    ModelErrorCode.STREAM_INCOMPLETE,
                    "model stream contains events after its terminal",
                )
            if delta.completion_status is not None:
                terminal_seen = True
                terminal = delta
            yield delta
        if not terminal_seen:
            raise protocol_error(
                ModelErrorCode.STREAM_INCOMPLETE,
                "model stream ended without a terminal event",
            )
        validate_completion(
            terminal.completion_status,
            terminal.incomplete_reason,
            terminal.error_message,
        )
        return
    arguments = {"model": model, "messages": messages, "tools": tools}
    if max_output_tokens is not None:
        arguments["max_output_tokens"] = max_output_tokens
    if response_format is not None:
        arguments["response_format"] = response_format
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
    yield response_terminal(response)
    validate_completion(
        response.completion_status, response.incomplete_reason, response.error_message
    )


def response_terminal(response: ModelResponse) -> ModelDelta:
    return ModelDelta(
        usage=response.usage,
        completion_status=response.completion_status or "completed",
        incomplete_reason=response.incomplete_reason,
        error_message=response.error_message,
    )


def _usage(raw: Any) -> ModelUsage:
    """Normalize Chat Completions and Responses usage objects.

    Providers vary between object and mapping responses, and Responses may put
    cache/reasoning details in nested fields. The top-level input/output values
    are authoritative; nested values are used only when a top-level value is
    absent so reasoning tokens are not silently dropped.
    """

    def value(source: Any, *names: str) -> Any:
        if source is None:
            return None
        for name in names:
            if isinstance(source, dict):
                item = source.get(name)
            else:
                item = getattr(source, name, None)
            if item is not None:
                return item
        return None

    if raw is None:
        return ModelUsage(source="unknown")
    input_value = value(raw, "prompt_tokens", "input_tokens")
    output_value = value(raw, "completion_tokens", "output_tokens")
    reported = input_value is not None or output_value is not None
    if output_value is None:
        details = value(raw, "output_tokens_details", "completion_tokens_details")
        output_value = value(details, "reasoning_tokens", "reasoning") or 0
    if input_value is None:
        details = value(raw, "input_tokens_details", "prompt_tokens_details")
        input_value = value(details, "cached_tokens", "cache_read_input_tokens") or 0
    cached = value(
        value(raw, "input_tokens_details", "prompt_tokens_details"),
        "cached_tokens",
        "cache_read_input_tokens",
    )
    reasoning = value(
        value(raw, "output_tokens_details", "completion_tokens_details"),
        "reasoning_tokens",
        "reasoning",
    )
    return ModelUsage(
        int(input_value or 0),
        int(output_value or 0),
        int(cached) if cached is not None else None,
        int(reasoning) if reasoning is not None else None,
        source="provider" if reported else "unknown",
    )


def _responses_format(response_format: dict[str, object]) -> dict[str, object]:
    kind = response_format.get("type")
    if kind == "json_schema":
        definition = response_format.get("json_schema")
        if not isinstance(definition, dict):
            raise ValueError("invalid JSON Schema response format")
        return {
            "type": "json_schema",
            "name": definition.get("name"),
            "strict": bool(definition.get("strict", True)),
            "schema": definition.get("schema"),
        }
    if kind in {"json_object", "text"}:
        return {"type": kind}
    raise ValueError(f"unsupported Responses output format: {kind}")


def _responses_tools(
    tools: list[dict[str, object]], *, strict: bool
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for item in tools:
        function = item.get("function")
        if item.get("type") != "function" or not isinstance(function, dict):
            raise ValueError("Responses API only supports function tools")
        parameters = function.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("function tool parameters must be an object")
        output.append(
            {
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description"),
                "parameters": strict_provider_schema(parameters)
                if strict
                else parameters,
                "strict": strict,
            }
        )
    return output


def _responses_input(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        if role == "tool":
            output.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id", "")),
                    "output": _response_output_value(message.get("content")),
                }
            )
            continue
        content = message.get("content")
        if content is not None and content != "":
            output.append(
                {
                    "role": role,
                    "content": _responses_content(content, role=role),
                }
            )
        for call in message.get("tool_calls", ()) or ():
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            output.append(
                {
                    "type": "function_call",
                    "call_id": str(call.get("id", "")),
                    "name": str(function.get("name", "")),
                    "arguments": str(function.get("arguments", "") or ""),
                }
            )
    return output


def _responses_content(content: object, *, role: str) -> object:
    if not isinstance(content, list):
        return content
    output: list[dict[str, object]] = []
    for block in content:
        if not isinstance(block, dict):
            output.append({"type": "input_text", "text": str(block)})
            continue
        kind, value = block.get("type"), block.get("value")
        if kind == "text":
            output.append(
                {
                    "type": "output_text" if role == "assistant" else "input_text",
                    "text": str(value or ""),
                }
            )
        elif kind == "image":
            if isinstance(value, str):
                url, detail = value, None
            elif isinstance(value, dict):
                url = value.get("url", value.get("image_url", value.get("data")))
                detail = value.get("detail")
            else:
                url, detail = None, None
            if isinstance(url, str):
                part: dict[str, object] = {"type": "input_image", "image_url": url}
                if detail in {"auto", "low", "high", "original"}:
                    part["detail"] = detail
                output.append(part)
            else:
                output.append({"type": "input_text", "text": json.dumps(value)})
        else:
            output.append(
                {
                    "type": "input_text",
                    "text": json.dumps(
                        {"type": kind, "value": value},
                        ensure_ascii=False,
                        default=str,
                    ),
                }
            )
    return output


def _response_output_value(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _responses_response(response: Any) -> ModelResponse:
    content: list[str] = []
    reasoning: list[str] = []
    calls: list[ModelToolCall] = []
    for item in getattr(response, "output", ()) or ():
        kind = getattr(item, "type", None)
        if kind == "message":
            for part in getattr(item, "content", ()) or ():
                text = getattr(part, "text", None) or getattr(part, "refusal", None)
                if text:
                    content.append(str(text))
        elif kind == "reasoning":
            for part in (
                *(getattr(item, "content", ()) or ()),
                *(getattr(item, "summary", ()) or ()),
            ):
                text = getattr(part, "text", None)
                if text:
                    reasoning.append(str(text))
        elif kind == "function_call":
            calls.append(
                ModelToolCall(
                    str(getattr(item, "call_id", None) or getattr(item, "id", "")),
                    str(getattr(item, "name", "") or ""),
                    str(getattr(item, "arguments", "") or ""),
                )
            )
    if not content:
        text = getattr(response, "output_text", None)
        if text:
            content.append(str(text))
    return ModelResponse(
        ModelMessage(
            "".join(content) or None, tuple(calls), "".join(reasoning) or None
        ),
        replace(
            _usage(getattr(response, "usage", None)),
            request_id=_optional_string(getattr(response, "_request_id", None)),
        ),
        _optional_string(getattr(response, "status", None)),
        _incomplete_reason(response),
        _error_message(getattr(response, "error", None)),
    )
