"""Foreground async workflow TUI."""

from __future__ import annotations

import asyncio
import shlex
import shutil
from collections.abc import Callable
from dataclasses import replace

from prompt_toolkit.application import get_app_or_none, run_in_terminal
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from ..domain import (
    ActionRecord,
    AgentEvent,
    AgentEventKind,
    ApprovalDecision,
    BudgetRequest,
    BudgetSnapshot,
    MemoryScope,
    WorkItemStatus,
    RunMode,
)
from ..permissions import PermissionMode
from ..status import AgentStatus, status_for_event, status_message
from .context import CliContext
from .dispatch import dispatch_slash_command
from .prompt import (
    prompt_session,
    prompt_prelude,
    prompt_tokens,
    select_action_decision,
)
from .presentation import ToolPresentation, present_tool
from .views.common import error, startup
from .views.conversation import (
    approval_panel,
    assistant_content,
    assistant_label,
    reasoning_summary,
    system_message,
    tool_group,
    tool_result,
    user_message,
)
from .views.workflow import render_history, result_status


async def run_tui(context: CliContext, *, status_enabled: bool = True) -> int:
    agent, console = context.agent, context.console
    startup(
        console,
        workspace=str(agent.workspace),
        model=agent.model,
        session_id=agent.session_id,
        permission_mode=agent.permission_mode,
    )
    session = await agent.repositories.sessions.require(agent.session_id)
    render_history(
        console, session, await agent.repositories.sessions.transcript(agent.session_id)
    )
    state: dict[str, object] = {
        "task": None,
        "activity": None,
        "spinner_frame": 0,
        "status_enabled": status_enabled,
        "stopping": False,
        "details_expanded": False,
        "queued_items": {},
        "usage": (0, 0, 0.0),
    }

    def toggle_details() -> None:
        state["details_expanded"] = not bool(state.get("details_expanded"))

    def prelude() -> object:
        queued = state.get("queued_items")
        queued_items = tuple(queued.items()) if isinstance(queued, dict) else ()
        usage = state.get("usage")
        usage_value = usage if isinstance(usage, tuple) else (0, 0, 0.0)
        return prompt_prelude(
            queued_items=queued_items,
            activity=str(state["activity"]) if state.get("activity") else None,
            spinner_frame=int(state.get("spinner_frame", 0)),
            details_expanded=bool(state.get("details_expanded", False)),
            model=agent.model,
            permission=agent.permission_mode.value,
            workspace=str(agent.workspace),
            usage=usage_value,
        )

    inputs = prompt_session(
        lambda: [
            (entry.name, entry.package.description)
            for entry in agent.skills.entries()
            if entry.enabled and entry.error is None and entry.package is not None
        ],
        toggle_details=toggle_details,
        prelude_provider=prelude,
    )
    queue: asyncio.Queue[tuple[str, str, str | None] | None] = asyncio.Queue()
    state["inputs"] = inputs
    router = context.agent.chat_model

    async def authorize_budget(request: BudgetRequest) -> bool:
        prompt = (
            f"Model budget: {request.scope} {request.limit_type} "
            f"{request.current_value:.6g} + {request.reserved_value:.6g} "
            f"> {request.limit_value:.6g} using {request.profile}. "
            "Allow this model call? [y/N] "
        )
        answer = await run_in_terminal(lambda: console.input(prompt))
        return str(answer).strip().casefold() in {"y", "yes"}

    async def authorize_limit(snapshot: BudgetSnapshot) -> bool:
        used = snapshot.as_dict()["used"]
        prompt = (
            f"Soft limit reached: {used['tool_rounds']} rounds, "
            f"{used['tool_calls']} tool calls, {used['tokens']} tokens, "
            f"${float(used['budget_usd']):.6f}, {int(used['duration_ms'])}ms. "
            "Continue 32 more rounds? [y/N] "
        )
        answer = await run_in_terminal(lambda: console.input(prompt))
        return str(answer).strip().casefold() in {"y", "yes"}

    state["limit_authorizer"] = authorize_limit

    async def authorize_action(action: ActionRecord) -> ApprovalDecision:
        return await _authorize_action(context, action)

    set_authorizer = getattr(router, "set_budget_authorizer", None)
    if callable(set_authorizer):
        set_authorizer(authorize_budget)
    set_action_authorizer = getattr(agent, "set_action_authorizer", None)
    if callable(set_action_authorizer):
        set_action_authorizer(authorize_action)
    worker = asyncio.create_task(_worker(context, queue, state))
    animator = asyncio.create_task(_animate_activity(inputs, state))
    try:
        while True:
            try:
                with patch_stdout(raw=True):
                    question = (
                        await inputs.prompt_async(
                            prompt_tokens(agent.permission_mode),
                            show_frame=True,
                        )
                    ).strip()
            except KeyboardInterrupt:
                task = state.get("task")
                if isinstance(task, asyncio.Task) and not task.done():
                    task.cancel()
                    console.print("\n[warning]Cancelling the active run...[/]")
                    continue
                return 0
            except EOFError:
                return 0
            if not question:
                continue
            if question.startswith("/"):
                try:
                    if question.startswith("/queue retry "):
                        await _retry(context, queue, shlex.split(question)[2])
                    elif question.startswith("/queue start "):
                        await _start_queued(context, queue, shlex.split(question)[2])
                    elif await dispatch_slash_command(context, question) == "exit":
                        return 0
                except (ValueError, OSError) as exc:
                    error(console, exc)
                continue
            if agent.permission_mode is PermissionMode.ASK_FOR_APPROVAL:
                answer = await asyncio.to_thread(
                    console.input, "Send this request? [y/N] "
                )
                if answer.strip().casefold() not in {"y", "yes"}:
                    continue
            console.print(user_message(question))
            item = await agent.enqueue(question)
            queued_items = state.get("queued_items")
            if isinstance(queued_items, dict):
                queued_items[item.id] = item.question
            await queue.put((item.id, item.question, None))
            console.print(
                result_status("Queued", "waiting", detail=item.id[:8])
            )
    finally:
        if callable(set_authorizer):
            set_authorizer(None)
        if callable(set_action_authorizer):
            set_action_authorizer(None)
        state["stopping"] = True
        await queue.put(None)
        worker.cancel()
        animator.cancel()
        for task in (worker, animator):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await _delete_empty_session(context)


async def _animate_activity(inputs: object, state: dict[str, object]) -> None:
    while not state.get("stopping"):
        await asyncio.sleep(0.08)
        if not state.get("activity"):
            continue
        state["spinner_frame"] = int(state.get("spinner_frame", 0)) + 1
        app = getattr(inputs, "app", None)
        if app is not None and app.is_running:
            app.invalidate()


async def _worker(
    context: CliContext, queue: asyncio.Queue, state: dict[str, object]
) -> None:
    while True:
        request = await queue.get()
        if request is None:
            return
        item_id, question, resume_from = request
        queued_items = state.get("queued_items")
        if isinstance(queued_items, dict):
            queued_items.pop(item_id, None)
        task = asyncio.create_task(
            _run_request(context, item_id, question, resume_from, state)
        )
        state["task"] = task
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await _TerminalWriter(context.console).print(
                result_status("Run failed", "failed", detail=str(exc))
            )
        finally:
            state["task"] = None
            _set_activity(state, None)


async def _run_request(
    context: CliContext,
    item_id: str,
    question: str,
    resume_from: str | None,
    state: dict[str, object],
) -> None:
    renderer = _RunRenderer(_TerminalWriter(context.console), state)
    _set_activity(state, status_message(AgentStatus.THINKING))
    try:
        async for event in context.agent.ask_stream(
            question,
            work_item_id=item_id,
            resume_from_run_id=resume_from,
            mode=RunMode.INTERACTIVE,
            authorize_limit=state.get("limit_authorizer"),
        ):
            await renderer.handle(event)
    except asyncio.CancelledError:
        await renderer.cancel()
        raise
    except Exception as exc:
        if not renderer.terminal:
            await renderer.fail(str(exc) or type(exc).__name__)
    finally:
        _set_activity(state, None)


async def _authorize_action(
    context: CliContext, action: ActionRecord
) -> ApprovalDecision:
    def choose() -> ApprovalDecision:
        size = shutil.get_terminal_size(fallback=(80, 24))
        if size.columns < 48 or size.lines < 14:
            context.console.print(
                "[warning]Terminal too small for safe approval; "
                "at least 48 columns × 14 rows are required.[/]"
            )
            return ApprovalDecision.REJECT
        context.console.print(approval_panel(action))
        return select_action_decision(action)

    try:
        return ApprovalDecision(await run_in_terminal(choose, in_executor=True))
    except (EOFError, KeyboardInterrupt):
        return ApprovalDecision.REJECT


class _RunRenderer:
    def __init__(self, writer: "_TerminalWriter", state: dict[str, object]) -> None:
        self.writer = writer
        self.state = state
        self.thinking = False
        self.reasoning_text = ""
        self.reasoning_emitted = 0
        self.answer_started = False
        self.active_tools: dict[str, ToolPresentation] = {}
        self.pending_tools: list[ToolPresentation] = []
        self.terminal = False

    async def handle(self, event: AgentEvent) -> None:
        if event.kind is AgentEventKind.THINKING:
            await self._thinking(str(event.data.get("text", "")))
        elif event.kind is AgentEventKind.TEXT_DELTA:
            await self._answer(str(event.data.get("text", "")))
        elif event.kind is AgentEventKind.TOOL_RUNNING:
            await self._tool_running(event)
        elif event.kind is AgentEventKind.TOOL_COMPLETED:
            await self._tool_completed(event)
        elif event.kind in {
            AgentEventKind.BUDGET_UPDATED,
            AgentEventKind.BUDGET_EXTENDED,
        }:
            budget = event.data.get("budget", {})
            used = budget.get("used", {}) if isinstance(budget, dict) else {}
            _set_activity(
                self.state,
                f"Budget: {used.get('tool_rounds', 0)} rounds / "
                f"{used.get('tool_calls', 0)} calls",
            )
        elif event.kind is AgentEventKind.LIMIT_REACHED:
            _set_activity(self.state, "Waiting for run-limit decision...")
        elif event.terminal:
            await self._terminal(event)

    async def _thinking(self, text: str) -> None:
        if not self.thinking:
            self.thinking = True
            _set_activity(self.state, status_message(AgentStatus.THINKING))
        if text:
            self.reasoning_text += text
            if bool(self.state.get("details_expanded")):
                if self.reasoning_emitted == 0:
                    await self.writer.print("\n[reasoning]◇ Reasoning[/]")
                pending = self.reasoning_text[self.reasoning_emitted :]
                self.reasoning_emitted = len(self.reasoning_text)
                await self.writer.write_text(pending, style="reasoning")

    async def _finish_thinking(self, status: str = "success") -> None:
        if not self.thinking:
            return
        expanded = bool(self.state.get("details_expanded"))
        if self.reasoning_text:
            if expanded and self.reasoning_emitted < len(self.reasoning_text):
                if self.reasoning_emitted == 0:
                    await self.writer.print("\n[reasoning]◇ Reasoning[/]")
                await self.writer.write_text(
                    self.reasoning_text[self.reasoning_emitted :], style="reasoning"
                )
                self.reasoning_emitted = len(self.reasoning_text)
            await self.writer.flush()
            outcome = {
                "success": "complete",
                "failed": "failed",
                "cancelled": "cancelled",
            }.get(status, status)
            await self.writer.print(reasoning_summary(self.reasoning_text, status=outcome))
        _set_activity(self.state, None)
        self.thinking = False
        self.reasoning_text = ""
        self.reasoning_emitted = 0

    async def _answer(self, text: str) -> None:
        await self._finish_thinking()
        await self._flush_tool_group()
        if not self.answer_started:
            await self.writer.print()
            await self.writer.print(assistant_label())
            self.answer_started = True
        _set_activity(self.state, None)
        await self.writer.write_markdown(text)

    async def _tool_running(self, event: AgentEvent) -> None:
        await self._finish_thinking()
        await self.writer.flush()
        self.answer_started = False
        item = present_tool(event.data, sequence=event.sequence)
        self.active_tools[item.identifier] = item
        if not item.groupable:
            await self._flush_tool_group()
        status, detail = status_for_event(
            AgentEventKind.TOOL_RUNNING, {"name": item.name}
        )
        _set_activity(self.state, status_message(status, detail))

    async def _tool_completed(self, event: AgentEvent) -> None:
        await self.writer.flush()
        completed = present_tool(event.data, sequence=event.sequence)
        running = self.active_tools.pop(completed.identifier, None)
        if running is None and "tool_call_id" not in event.data:
            running_id = next(
                (
                    identifier
                    for identifier, candidate in reversed(self.active_tools.items())
                    if candidate.name == completed.name
                ),
                None,
            )
            if running_id is not None:
                running = self.active_tools.pop(running_id)
        if running is not None:
            item = replace(
                running,
                title=completed.title or running.title,
                detail=completed.detail or running.detail,
                target=completed.target or running.target,
                outcome=completed.outcome,
                ok=completed.ok,
                duration_ms=completed.duration_ms,
            )
        else:
            item = completed
        if item.groupable:
            self.pending_tools.append(item)
            if item.ok is False:
                await self._flush_tool_group()
        else:
            await self._flush_tool_group()
            await self.writer.print(tool_result(item))
        _set_activity(self.state, status_message(AgentStatus.ANALYZING))

    async def _flush_tool_group(self) -> None:
        if not self.pending_tools:
            return
        items, self.pending_tools = self.pending_tools, []
        await self.writer.print(
            tool_group(items, expanded=bool(self.state.get("details_expanded")))
        )

    async def _terminal(self, event: AgentEvent) -> None:
        thinking_status = (
            "failed"
            if event.kind is AgentEventKind.FAILED
            else "cancelled"
            if event.kind is AgentEventKind.CANCELLED
            else "success"
        )
        await self._finish_thinking(thinking_status)
        await self._flush_tool_group()
        await self.writer.flush()
        _set_activity(self.state, None)
        self.terminal = True
        if event.kind is AgentEventKind.COMPLETED:
            if not self.answer_started and event.data.get("answer"):
                await self.writer.print()
                await self.writer.print(assistant_label())
                await self.writer.write_markdown(str(event.data["answer"]))
                await self.writer.flush()
            usage = event.data.get("usage", {})
            models = event.data.get("models", [])
            selected_model = ""
            if isinstance(models, list) and models:
                selected_model = (
                    f" · {models[0].get('provider', '-')}/{models[0].get('model', '-')}"
                )
            detail = (
                f"run {event.run_id[:8]} · {event.data.get('duration_ms', 0)}ms · "
                f"{usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)} tokens · "
                f"${float(usage.get('cost_usd', 0)):.6f}{selected_model}"
            )
            self.state["usage"] = (
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                float(usage.get("cost_usd", 0)),
            )
            await self.writer.print(
                result_status("Completed", "success", detail=detail)
            )
        elif event.kind is AgentEventKind.WAITING_APPROVAL:
            count = int(event.data.get("count", len(event.data.get("action_ids", []))))
            await self.writer.print(
                system_message(
                    result_status(
                        "Waiting for approval",
                        "waiting",
                        detail=f"{count} action(s) · /approvals",
                    ),
                    status="waiting",
                )
            )
        elif event.kind is AgentEventKind.CANCELLED:
            error = event.data.get("error", {})
            await self.writer.print(
                system_message(
                    result_status(
                        "Cancelled",
                        "cancelled",
                        detail=str(error.get("message", "")),
                    ),
                    status="cancelled",
                )
            )
        elif event.kind is AgentEventKind.STOPPED:
            reason = str(event.data.get("stop_reason", "stopped"))
            await self.writer.print(
                system_message(
                    result_status("Stopped", "stopped", detail=reason),
                    status="stopped",
                )
            )
        else:
            error = event.data.get("error", {})
            await self.writer.print(
                system_message(
                    result_status(
                        "Failed", "failed", detail=str(error.get("message", ""))
                    ),
                    status="failed",
                )
            )

    async def cancel(self) -> None:
        if self.terminal:
            return
        await self._finish_thinking("cancelled")
        await self._flush_tool_group()
        await self.writer.flush()
        _set_activity(self.state, None)
        await self.writer.print(
            system_message(
                result_status("Cancelled", "cancelled"), status="cancelled"
            )
        )
        self.terminal = True

    async def fail(self, message: str) -> None:
        await self._finish_thinking("failed")
        await self._flush_tool_group()
        await self.writer.flush()
        _set_activity(self.state, None)
        await self.writer.print(
            system_message(
                result_status("Failed", "failed", detail=message), status="failed"
            )
        )
        self.terminal = True


class _TerminalWriter:
    """Write above an active prompt so redraws cannot erase streamed output."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.buffer = ""
        self.markdown_buffer = ""
        self.style: str | None = None

    async def call(
        self, function: Callable[..., None], *args: object, **kwargs: object
    ) -> None:
        def invoke() -> None:
            function(*args, **kwargs)

        app = get_app_or_none()
        if app is not None and app.is_running:
            await run_in_terminal(invoke)
        else:
            invoke()

    async def print(self, *args: object, **kwargs: object) -> None:
        await self.call(self.console.print, *args, **kwargs)

    async def write_text(self, text: str, *, style: str | None = None) -> None:
        if self.markdown_buffer or (self.buffer and style != self.style):
            await self.flush()
        self.style = style
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            await self.print(
                line,
                style=self.style,
                markup=False,
                highlight=False,
            )

    async def write_markdown(self, text: str) -> None:
        if self.buffer:
            await self.flush()
        self.markdown_buffer += text
        boundary = _markdown_boundary(self.markdown_buffer)
        if boundary:
            complete, self.markdown_buffer = (
                self.markdown_buffer[:boundary],
                self.markdown_buffer[boundary:],
            )
            if complete.strip():
                await self.print(assistant_content(complete.rstrip()))

    async def flush(self) -> None:
        if self.buffer:
            text, self.buffer = self.buffer, ""
            style, self.style = self.style, None
            await self.print(text, style=style, markup=False, highlight=False)
        else:
            self.style = None
        if self.markdown_buffer:
            text, self.markdown_buffer = self.markdown_buffer, ""
            if text.strip():
                await self.print(assistant_content(text))


def _markdown_boundary(value: str) -> int:
    """Return the last complete paragraph boundary outside fenced code."""

    fenced = False
    offset = 0
    boundary = 0
    for line in value.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
        offset += len(line)
        if not fenced and not line.strip():
            boundary = offset
    return boundary


def _set_activity(state: dict[str, object], activity: str | None) -> None:
    if not state.get("status_enabled", True):
        activity = None
    state["activity"] = activity
    if activity is None:
        state["spinner_frame"] = 0
    inputs = state.get("inputs")
    app = getattr(inputs, "app", None)
    if app is not None and app.is_running:
        app.invalidate()


async def _retry(context: CliContext, queue: asyncio.Queue, prefix: str) -> None:
    run = await context.agent.repositories.workflow.retryable_run(
        context.agent.session_id, prefix
    )
    item = await context.agent.enqueue(
        run.question, parent_work_item_id=run.work_item_id
    )
    await queue.put((item.id, item.question, run.id))
    context.console.print(f"[waiting]Retry queued:[/] {run.id[:8]}")


async def _start_queued(context: CliContext, queue: asyncio.Queue, prefix: str) -> None:
    repository = context.agent.repositories.workflow
    item = await repository.require_work_item(prefix)
    if item.session_id != context.agent.session_id:
        raise ValueError("work item does not belong to this session")
    if item.status is not WorkItemStatus.QUEUED:
        raise ValueError("only queued work can be started")
    await queue.put((item.id, item.question, None))
    context.console.print(f"[waiting]Queued work activated:[/] {item.id[:8]}")


async def _delete_empty_session(context: CliContext) -> None:
    memory = context.agent.memory
    if memory is not None:
        try:
            if await memory.list(
                scope=MemoryScope.SESSION, include_inactive=True, limit=1
            ):
                return
        except Exception:
            return
    await context.agent.repositories.sessions.delete_if_empty(context.agent.session_id)
