"""Foreground async workflow TUI."""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Callable

from prompt_toolkit.application import get_app_or_none, run_in_terminal
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from ..domain import AgentEvent, AgentEventKind, BudgetRequest, MemoryScope
from ..permissions import PermissionMode
from ..status import AgentStatus, status_for_event, status_message
from .context import CliContext
from .dispatch import dispatch_slash_command
from .prompt import permission_rprompt, prompt_footer, prompt_session, prompt_tokens
from .views.common import error, startup
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
    inputs = prompt_session(
        lambda: [
            (entry.name, entry.package.description)
            for entry in agent.skills.entries()
            if entry.enabled and entry.error is None and entry.package is not None
        ]
    )
    queue: asyncio.Queue[tuple[str, str, str | None] | None] = asyncio.Queue()
    state: dict[str, object] = {
        "task": None,
        "activity": None,
        "spinner_frame": 0,
        "status_enabled": status_enabled,
        "stopping": False,
        "inputs": inputs,
    }
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

    set_authorizer = getattr(router, "set_budget_authorizer", None)
    if callable(set_authorizer):
        set_authorizer(authorize_budget)
    worker = asyncio.create_task(_worker(context, queue, state))
    animator = asyncio.create_task(_animate_activity(inputs, state))
    try:
        while True:
            try:
                with patch_stdout(raw=True):
                    question = (
                        await inputs.prompt_async(
                            prompt_tokens(agent.permission_mode),
                            rprompt=permission_rprompt(agent.permission_mode),
                            bottom_toolbar=lambda: prompt_footer(
                                activity=str(state["activity"])
                                if state.get("activity")
                                else None,
                                spinner_frame=int(state.get("spinner_frame", 0)),
                            ),
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
            item = await agent.enqueue(question)
            await queue.put((item.id, item.question, None))
            console.print(
                f"[waiting]Queued:[/] {item.question} [text.muted]({item.id[:8]})[/]"
            )
    finally:
        if callable(set_authorizer):
            set_authorizer(None)
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
            question, work_item_id=item_id, resume_from_run_id=resume_from
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


class _RunRenderer:
    def __init__(self, writer: "_TerminalWriter", state: dict[str, object]) -> None:
        self.writer = writer
        self.state = state
        self.thinking = False
        self.reasoning_started = False
        self.answer_started = False
        self.active_tool: str | None = None
        self.terminal = False

    async def handle(self, event: AgentEvent) -> None:
        if event.kind is AgentEventKind.THINKING:
            await self._thinking(str(event.data.get("text", "")))
        elif event.kind is AgentEventKind.TEXT_DELTA:
            await self._answer(str(event.data.get("text", "")))
        elif event.kind is AgentEventKind.TOOL_RUNNING:
            await self._tool_running(str(event.data.get("name", "unknown")))
        elif event.kind is AgentEventKind.TOOL_COMPLETED:
            await self._tool_completed(event)
        elif event.terminal:
            await self._terminal(event)

    async def _thinking(self, text: str) -> None:
        if not self.thinking:
            self.thinking = True
            _set_activity(self.state, status_message(AgentStatus.THINKING))
        if text:
            if not self.reasoning_started:
                await self.writer.print("\n[reasoning]◇ Model reasoning[/]")
                self.reasoning_started = True
            await self.writer.write_text(text, style="reasoning")

    async def _finish_thinking(self, status: str = "success") -> None:
        if not self.thinking:
            return
        await self.writer.flush()
        _set_activity(self.state, None)
        outcome = {
            "success": "complete",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(status, status)
        label = "Reasoning" if self.reasoning_started else "Thinking"
        await self.writer.print(result_status(f"{label} {outcome}", status))
        self.thinking = False
        self.reasoning_started = False

    async def _answer(self, text: str) -> None:
        await self._finish_thinking()
        if not self.answer_started:
            await self.writer.print("\n[agent.bold]◆ CapsLock[/]")
            self.answer_started = True
        await self.writer.write_text(text, style="answer")

    async def _tool_running(self, name: str) -> None:
        await self._finish_thinking()
        await self.writer.flush()
        self.answer_started = False
        self.active_tool = name
        status, detail = status_for_event(AgentEventKind.TOOL_RUNNING, {"name": name})
        _set_activity(self.state, status_message(status, detail))

    async def _tool_completed(self, event: AgentEvent) -> None:
        await self.writer.flush()
        _set_activity(self.state, status_message(AgentStatus.ANALYZING))
        name = str(event.data.get("name", self.active_tool or "unknown"))
        ok = bool(event.data.get("ok"))
        duration = int(event.data.get("duration_ms", 0))
        await self.writer.print(
            result_status(
                f"Tool {name} {'completed' if ok else 'failed'}",
                "success" if ok else "failed",
                detail=f"{duration}ms",
            )
        )
        self.active_tool = None

    async def _terminal(self, event: AgentEvent) -> None:
        thinking_status = (
            "failed"
            if event.kind is AgentEventKind.FAILED
            else "cancelled"
            if event.kind is AgentEventKind.CANCELLED
            else "success"
        )
        await self._finish_thinking(thinking_status)
        await self.writer.flush()
        _set_activity(self.state, None)
        self.terminal = True
        if event.kind is AgentEventKind.COMPLETED:
            if not self.answer_started and event.data.get("answer"):
                await self.writer.print("\n[agent.bold]◆ CapsLock[/]")
                await self.writer.print(
                    str(event.data["answer"]),
                    style="answer",
                    markup=False,
                    highlight=False,
                )
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
            await self.writer.print(
                result_status("Completed", "success", detail=detail)
            )
        elif event.kind is AgentEventKind.WAITING_APPROVAL:
            count = int(event.data.get("count", len(event.data.get("action_ids", []))))
            await self.writer.print(
                result_status(
                    "Waiting for approval",
                    "waiting",
                    detail=f"{count} action(s) · /approvals",
                )
            )
        elif event.kind is AgentEventKind.CANCELLED:
            error = event.data.get("error", {})
            await self.writer.print(
                result_status(
                    "Cancelled", "cancelled", detail=str(error.get("message", ""))
                )
            )
        else:
            error = event.data.get("error", {})
            await self.writer.print(
                result_status("Failed", "failed", detail=str(error.get("message", "")))
            )

    async def cancel(self) -> None:
        if self.terminal:
            return
        await self._finish_thinking("cancelled")
        await self.writer.flush()
        _set_activity(self.state, None)
        await self.writer.print(result_status("Cancelled", "cancelled"))
        self.terminal = True

    async def fail(self, message: str) -> None:
        await self._finish_thinking("failed")
        await self.writer.flush()
        _set_activity(self.state, None)
        await self.writer.print(result_status("Failed", "failed", detail=message))
        self.terminal = True


class _TerminalWriter:
    """Write above an active prompt so redraws cannot erase streamed output."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.buffer = ""
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
        if self.buffer and style != self.style:
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

    async def flush(self) -> None:
        if not self.buffer:
            self.style = None
            return
        text, self.buffer = self.buffer, ""
        style, self.style = self.style, None
        await self.print(text, style=style, markup=False, highlight=False)


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
