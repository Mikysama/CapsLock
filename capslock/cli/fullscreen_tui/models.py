"""Immutable view models and the AgentEvent reducer for the fullscreen TUI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from ...domain import AgentEvent, AgentEventKind


class MessageKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOLS = "tools"
    SYSTEM = "system"


@dataclass(frozen=True)
class ToolViewModel:
    id: str
    name: str
    category: str
    title: str
    detail: str | None = None
    target: str | None = None
    status: str = "running"
    duration_ms: int | None = None


@dataclass(frozen=True)
class MessageViewModel:
    id: str
    kind: MessageKind
    text: str
    run_id: str | None = None
    collapsed: bool = False
    status: str | None = None
    tools: tuple[ToolViewModel, ...] = ()


@dataclass(frozen=True)
class QueueViewModel:
    id: str
    text: str
    status: str = "queued"


@dataclass(frozen=True)
class UsageViewModel:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


@dataclass(frozen=True)
class TuiState:
    messages: tuple[MessageViewModel, ...] = ()
    queue: tuple[QueueViewModel, ...] = ()
    active_run_id: str | None = None
    activity: str | None = None
    details_expanded: bool = False
    terminal_runs: frozenset[str] = frozenset()
    usage: UsageViewModel = UsageViewModel()
    notification: str | None = None

    @property
    def has_streaming_answer(self) -> bool:
        return bool(
            self.messages
            and self.messages[-1].kind is MessageKind.ASSISTANT
            and self.messages[-1].run_id == self.active_run_id
            and self.messages[-1].text
        )


def history_state(transcript: list[dict[str, Any]]) -> TuiState:
    messages = []
    for index, entry in enumerate(transcript):
        role = str(entry.get("role", "assistant"))
        kind = MessageKind.USER if role == "user" else MessageKind.ASSISTANT
        messages.append(
            MessageViewModel(
                f"history-{index}",
                kind,
                str(entry.get("content", "")),
                status=str(entry.get("status")) if entry.get("status") else None,
            )
        )
    return TuiState(messages=tuple(messages))


def add_user_message(state: TuiState, identifier: str, text: str) -> TuiState:
    message = MessageViewModel(f"user-{identifier}", MessageKind.USER, text)
    queued = QueueViewModel(identifier, text)
    return replace(
        state,
        messages=(*state.messages, message),
        queue=(*state.queue, queued),
        notification=f"Queued {identifier[:8]}",
    )


def add_system_message(state: TuiState, text: str, *, status: str = "info") -> TuiState:
    identifier = f"system-{len(state.messages)}-{len(text)}"
    message = MessageViewModel(identifier, MessageKind.SYSTEM, text, status=status)
    return replace(state, messages=(*state.messages, message), notification=None)


def set_queue_running(
    state: TuiState, item_id: str, run_id: str | None = None
) -> TuiState:
    queue = tuple(
        replace(item, status="running") if item.id == item_id else item
        for item in state.queue
    )
    return replace(state, queue=queue, active_run_id=run_id, activity="Thinking")


def remove_queue_item(state: TuiState, item_id: str) -> TuiState:
    return replace(
        state,
        queue=tuple(item for item in state.queue if item.id != item_id),
        active_run_id=None,
        activity=None,
    )


def toggle_details(state: TuiState) -> TuiState:
    expanded = not state.details_expanded
    messages = tuple(
        replace(message, collapsed=not expanded)
        if message.kind in {MessageKind.REASONING, MessageKind.TOOLS}
        else message
        for message in state.messages
    )
    return replace(state, details_expanded=expanded, messages=messages)


def reduce_event(state: TuiState, event: AgentEvent) -> TuiState:
    if event.run_id in state.terminal_runs and not event.terminal:
        return state
    if event.kind is AgentEventKind.QUEUED:
        return replace(state, active_run_id=event.run_id, activity="Thinking")
    if event.kind is AgentEventKind.THINKING:
        return _append_text(state, event, MessageKind.REASONING, "Thinking")
    if event.kind is AgentEventKind.TEXT_DELTA:
        state = _collapse_reasoning(state, event.run_id)
        return _append_text(state, event, MessageKind.ASSISTANT, None)
    if event.kind is AgentEventKind.TOOL_RUNNING:
        return _tool_running(state, event)
    if event.kind is AgentEventKind.TOOL_COMPLETED:
        return _tool_completed(state, event)
    if event.kind is AgentEventKind.LIMIT_REACHED:
        return replace(state, activity="Waiting for run-limit decision")
    if event.kind is AgentEventKind.BUDGET_EXTENDED:
        return replace(state, activity="Thinking")
    if event.kind in {AgentEventKind.BUDGET_UPDATED}:
        return state
    if event.terminal:
        return _terminal(state, event)
    return state


def _append_text(
    state: TuiState,
    event: AgentEvent,
    kind: MessageKind,
    activity: str | None,
) -> TuiState:
    text = str(event.data.get("text", ""))
    messages = list(state.messages)
    if messages and messages[-1].kind is kind and messages[-1].run_id == event.run_id:
        messages[-1] = replace(messages[-1], text=messages[-1].text + text)
    elif text:
        messages.append(
            MessageViewModel(
                f"{kind.value}-{event.run_id}-{event.sequence}",
                kind,
                text,
                event.run_id,
                collapsed=False,
            )
        )
    return replace(
        state,
        messages=tuple(messages),
        active_run_id=event.run_id,
        activity=activity,
    )


def _presentation(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get("presentation")
    if not isinstance(value, dict) or value.get("version") != 1:
        return {"category": "other", "title": name.replace("_", " ").capitalize()}
    return value


def _tool_running(state: TuiState, event: AgentEvent) -> TuiState:
    name = str(event.data.get("name", "unknown"))
    presentation = _presentation(event.data, name)
    tool = ToolViewModel(
        str(event.data.get("tool_call_id", f"tool-{event.sequence}")),
        name,
        str(presentation.get("category", "other")),
        str(presentation.get("title", name)),
        str(presentation["detail"]) if presentation.get("detail") else None,
        str(presentation["target"]) if presentation.get("target") else None,
    )
    messages = list(_collapse_reasoning(state, event.run_id).messages)
    groupable = tool.category in {"read", "search"}
    if (
        groupable
        and messages
        and messages[-1].kind is MessageKind.TOOLS
        and messages[-1].run_id == event.run_id
        and all(item.category in {"read", "search"} for item in messages[-1].tools)
    ):
        messages[-1] = replace(messages[-1], tools=(*messages[-1].tools, tool))
    else:
        messages.append(
            MessageViewModel(
                f"tools-{event.run_id}-{event.sequence}",
                MessageKind.TOOLS,
                "",
                event.run_id,
                collapsed=groupable and not state.details_expanded,
                tools=(tool,),
            )
        )
    return replace(
        state,
        messages=tuple(messages),
        active_run_id=event.run_id,
        activity=f"Running {tool.title}",
    )


def _tool_completed(state: TuiState, event: AgentEvent) -> TuiState:
    identifier = str(event.data.get("tool_call_id", ""))
    ok = bool(event.data.get("ok"))
    duration = int(event.data.get("duration_ms", 0))
    messages = []
    for message in state.messages:
        if message.kind is not MessageKind.TOOLS:
            messages.append(message)
            continue
        tools = tuple(
            replace(
                tool,
                status="success" if ok else "failed",
                duration_ms=duration,
            )
            if tool.id == identifier
            else tool
            for tool in message.tools
        )
        important_failure = not ok and any(
            tool.id == identifier for tool in message.tools
        )
        messages.append(
            replace(
                message,
                tools=tools,
                collapsed=False if important_failure else message.collapsed,
            )
        )
    return replace(state, messages=tuple(messages), activity="Thinking")


def _collapse_reasoning(state: TuiState, run_id: str) -> TuiState:
    messages = tuple(
        replace(message, collapsed=not state.details_expanded)
        if message.kind is MessageKind.REASONING and message.run_id == run_id
        else message
        for message in state.messages
    )
    return replace(state, messages=messages)


def _terminal(state: TuiState, event: AgentEvent) -> TuiState:
    messages = list(_collapse_reasoning(state, event.run_id).messages)
    unfinished_status = (
        "cancelled"
        if event.kind in {AgentEventKind.CANCELLED, AgentEventKind.STOPPED}
        else "failed"
    )
    messages = [
        replace(
            message,
            tools=tuple(
                replace(tool, status=unfinished_status)
                if tool.status == "running"
                else tool
                for tool in message.tools
            ),
            collapsed=False
            if event.kind is not AgentEventKind.COMPLETED
            and any(tool.status == "running" for tool in message.tools)
            else message.collapsed,
        )
        if message.kind is MessageKind.TOOLS and message.run_id == event.run_id
        else message
        for message in messages
    ]
    if event.kind in {AgentEventKind.COMPLETED, AgentEventKind.STOPPED}:
        answer = str(event.data.get("answer", ""))
        has_answer = any(
            message.kind is MessageKind.ASSISTANT and message.run_id == event.run_id
            for message in messages
        )
        if answer and not has_answer:
            messages.append(
                MessageViewModel(
                    f"assistant-{event.run_id}-terminal",
                    MessageKind.ASSISTANT,
                    answer,
                    event.run_id,
                )
            )
    status = event.kind.value
    if event.kind is not AgentEventKind.COMPLETED:
        error = event.data.get("error")
        detail = error.get("message", "") if isinstance(error, dict) else ""
        if event.kind is AgentEventKind.STOPPED:
            detail = str(event.data.get("stop_reason", "stopped"))
        messages.append(
            MessageViewModel(
                f"terminal-{event.run_id}",
                MessageKind.SYSTEM,
                f"{status.replace('_', ' ').title()}{': ' + detail if detail else ''}",
                event.run_id,
                status=status,
            )
        )
    usage = event.data.get("usage", {})
    view = UsageViewModel(
        int(usage.get("input_tokens", 0)) if isinstance(usage, dict) else 0,
        int(usage.get("output_tokens", 0)) if isinstance(usage, dict) else 0,
        float(usage.get("cost_usd", 0)) if isinstance(usage, dict) else 0.0,
        int(event.data.get("duration_ms", 0)),
    )
    return replace(
        state,
        messages=tuple(messages),
        active_run_id=None,
        activity=None,
        terminal_runs=state.terminal_runs | {event.run_id},
        usage=view,
        notification=status.replace("_", " ").title(),
    )
