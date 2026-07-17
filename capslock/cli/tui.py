"""Inline workflow TUI built on the existing prompt-toolkit stack."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from prompt_toolkit.application import get_app_or_none, run_in_terminal
from prompt_toolkit.patch_stdout import patch_stdout

from ..domain import AgentEventKind, ActionType, WorkItemStatus
from ..permissions import PermissionMode
from ..runtime import AgentRuntimeError
from ..runtime_support import CancellationToken
from . import actions
from .chat import WEB_CONTINUATION, _delete_empty_session
from .context import CliContext
from .dispatch import dispatch_slash_command
from .prompt import permission_rprompt, prompt_footer, prompt_session, prompt_tokens
from .render import (
    render_answer_metadata,
    render_session_history,
    render_workflow_status,
    render_work_queue,
    select_choice,
    startup_banner,
)


def run_tui(context: CliContext, debug: bool) -> int:
    try:
        return asyncio.run(_run_tui(context, debug))
    finally:
        _delete_empty_session(context.agent)


async def _run_tui(context: CliContext, debug: bool) -> int:
    agent, console = context.agent, context.console
    console.print(startup_banner(agent))
    render_session_history(console, agent)
    inputs = prompt_session(
        lambda: [
            (entry.name, entry.package.description)
            for entry in agent.skills.entries()
            if entry.enabled and entry.error is None and entry.package is not None
        ]
    )
    pending: list[tuple[str, str, str | None]] = []
    wake = asyncio.Event()
    state: dict[str, object] = {
        "token": None,
        "stopping": False,
        "activity": None,
        "spinner_frame": 0,
    }
    worker = asyncio.create_task(_workflow_worker(context, pending, wake, state, debug))
    animator = asyncio.create_task(_animate_activity(inputs, state))
    try:
        while True:
            render_workflow_status(console, agent)
            try:
                with patch_stdout(raw=True):
                    question = (await inputs.prompt_async(
                        prompt_tokens(agent.permission_mode),
                        rprompt=permission_rprompt(agent.permission_mode),
                        bottom_toolbar=lambda: prompt_footer(
                            activity=str(state["activity"]) if state.get("activity") else None,
                            spinner_frame=int(state.get("spinner_frame", 0)),
                        ),
                    )).strip()
            except KeyboardInterrupt:
                token = state.get("token")
                if isinstance(token, CancellationToken) and not token.cancelled:
                    token.cancel()
                    console.print("\n[warning]Cancelling the active run...[/]")
                    continue
                console.print()
                return 0
            except EOFError:
                console.print()
                return 0
            if question.startswith("/"):
                try:
                    if await _dispatch_tui_command(context, question, pending, wake, state) == "exit":
                        return 0
                except (AgentRuntimeError, ValueError) as exc:
                    console.print(f"[error]Error:[/] {exc}")
                continue
            if not question:
                continue
            if agent.permission_mode is PermissionMode.ASK_FOR_APPROVAL:
                choice = select_choice(
                    console,
                    "Send this request to CapsLock?",
                    (("approve", "Approve and send"), ("reject", "Do not send")),
                    escape_key="reject",
                )
                if choice != "approve":
                    continue
            item = agent.enqueue(question)
            pending.append((item.id, item.question, None))
            wake.set()
            console.print(f"[waiting]Queued:[/] {item.question} [text.muted]({item.id[:8]})[/]")
    finally:
        state["stopping"] = True
        token = state.get("token")
        if isinstance(token, CancellationToken):
            token.cancel()
        worker.cancel()
        animator.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        try:
            await animator
        except asyncio.CancelledError:
            pass


async def _animate_activity(inputs: object, state: dict[str, object]) -> None:
    while not state.get("stopping"):
        await asyncio.sleep(0.08)
        if not state.get("activity"):
            continue
        state["spinner_frame"] = int(state.get("spinner_frame", 0)) + 1
        app = getattr(inputs, "app", None)
        if app is not None and app.is_running:
            app.invalidate()


async def _workflow_worker(
    context: CliContext,
    pending: list[tuple[str, str, str | None]],
    wake: asyncio.Event,
    state: dict[str, object],
    debug: bool,
) -> None:
    while not state.get("stopping"):
        if not pending:
            wake.clear()
            await wake.wait()
            continue
        item_id, question, resume_from_run_id = pending.pop(0)
        token = CancellationToken()
        state["token"] = token
        try:
            await _run_request(
                context,
                question,
                debug,
                work_item_id=item_id,
                token=token,
                resume_from_run_id=resume_from_run_id,
                set_activity=lambda activity: _set_activity(state, activity),
            )
        finally:
            state["token"] = None


async def _run_request(
    context: CliContext,
    question: str,
    debug: bool,
    *,
    work_item_id: str,
    token: CancellationToken,
    resume_from_run_id: str | None = None,
    set_activity: Callable[[str | None], None] | None = None,
) -> None:
    agent, console = context.agent, context.console
    writer = _TerminalWriter(console)
    printed = False
    failure_reported = False
    try:
        async for event in agent.ask_stream(
            question,
            work_item_id=work_item_id,
            resume_from_run_id=resume_from_run_id,
            cancellation=token,
        ):
            if event.kind is AgentEventKind.THINKING and not printed:
                if set_activity is not None:
                    set_activity("thinking")
                await writer.print("[running.bold]⠋[/] [thinking]Thinking...[/]")
            elif event.kind is AgentEventKind.TEXT_DELTA:
                if set_activity is not None:
                    set_activity(None)
                await writer.write_text(str(event.data.get("text", "")))
                printed = True
            elif event.kind is AgentEventKind.TOOL_RUNNING:
                if set_activity is not None:
                    set_activity(None)
                await writer.flush()
                printed = False
                await writer.print(f"[running]Running tool:[/] {event.data.get('name', 'unknown')}")
            elif event.kind is AgentEventKind.TOOL_COMPLETED:
                await writer.print(
                    f"[{'success' if event.data.get('ok') else 'error'}]Tool "
                    f"{event.data.get('name')} {'completed' if event.data.get('ok') else 'failed'}[/]"
                )
            elif event.kind is AgentEventKind.WAITING_APPROVAL:
                await writer.flush()
                await writer.print("[waiting]Waiting for approval.[/] Use [command]/approvals[/], [command]/approve <id>[/], or [command]/reject <id>[/].")
            elif event.kind is AgentEventKind.FAILED:
                await writer.flush()
                await writer.print(f"[error]Error:[/] {event.data.get('error')}")
                failure_reported = True
    except (KeyboardInterrupt, asyncio.CancelledError):
        token.cancel()
        await writer.flush()
        await writer.print("[warning]Cancelled.[/]")
        return
    except AgentRuntimeError as exc:
        if not failure_reported:
            await writer.flush()
            await writer.print(f"[error]Error:[/] {exc}")
        return
    except Exception as exc:
        if not failure_reported:
            await writer.flush()
            await writer.print(f"[error]Model or transport error:[/] {exc}")
        return
    finally:
        if set_activity is not None:
            set_activity(None)
    await writer.flush()
    if agent.last_answer is not None:
        await writer.call(render_answer_metadata, console, agent.last_answer, debug)


def _set_activity(state: dict[str, object], activity: str | None) -> None:
    state["activity"] = activity


class _TerminalWriter:
    """Write above an active prompt without letting its redraw erase model output."""

    def __init__(self, console: object) -> None:
        self.console = console
        self.buffer = ""

    async def call(self, function: Callable[..., None], *args: object, **kwargs: object) -> None:
        def invoke() -> None:
            function(*args, **kwargs)

        app = get_app_or_none()
        if app is not None and app.is_running:
            await run_in_terminal(invoke)
        else:
            invoke()

    async def print(self, *args: object, **kwargs: object) -> None:
        await self.call(self.console.print, *args, **kwargs)

    async def write_text(self, text: str) -> None:
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            await self.print(line, markup=False, highlight=False)

    async def flush(self) -> None:
        if not self.buffer:
            return
        text, self.buffer = self.buffer, ""
        await self.print(text, markup=False, highlight=False)


async def _dispatch_tui_command(
    context: CliContext,
    text: str,
    pending: list[tuple[str, str, str | None]],
    wake: asyncio.Event,
    state: dict[str, object],
) -> str:
    if text == "/exit" or text == "/quit":
        return "exit"
    if text.startswith("/approve "):
        await _approve_tui_action(context, text.partition(" ")[2].strip(), pending, wake)
        return "handled"
    if text == "/cancel":
        token = state.get("token")
        if isinstance(token, CancellationToken) and not token.cancelled:
            token.cancel()
            context.console.print("[warning]Cancelling the active run...[/]")
        else:
            context.console.print("[text.secondary]No active run.[/]")
        return "handled"
    if text.startswith("/queue"):
        _queue_command(context, text, pending)
        return "handled"
    if text.startswith("/retry "):
        _retry_command(context, text.partition(" ")[2].strip(), pending, wake)
        return "handled"
    return dispatch_slash_command(context, text)


async def _approve_tui_action(
    context: CliContext,
    action_id: str,
    pending: list[tuple[str, str, str | None]],
    wake: asyncio.Event,
) -> None:
    agent, console = context.agent, context.console
    item = actions.action_coordinator(agent).resolve(action_id)
    actions.render_approvals(context)
    choice = select_choice(
        console,
        f"{item.type.value} · {item.id[:12]}",
        (("approve", "Approve and run"), ("later", "Decide later")),
        escape_key="later",
    )
    if choice != "approve":
        return
    result = await actions.action_coordinator(agent, item.run_id).approve_and_execute_async(item.id)
    console.print(f"[success]Completed:[/] {result.id[:12]} · {item.type.value}")
    actions.settle_workflow(agent, result.run_id)
    if item.type in {ActionType.WEB_SEARCH, ActionType.WEB_FETCH}:
        continuation = agent.enqueue(WEB_CONTINUATION)
        pending.append((continuation.id, continuation.question, None))
        wake.set()


def _queue_command(
    context: CliContext,
    text: str,
    pending: list[tuple[str, str, str | None]],
) -> None:
    parts = text.split()
    if len(parts) == 1:
        render_work_queue(context.console, context.agent.store.list_work_items(context.agent.session_id, active_only=True))
        return
    if len(parts) == 3 and parts[1] == "cancel":
        matches = [index for index, item in enumerate(pending) if item[0].startswith(parts[2])]
        if len(matches) == 1:
            item_id, _, _ = pending.pop(matches[0])
        else:
            queued = [
                item for item in context.agent.store.list_work_items(context.agent.session_id, active_only=True)
                if item.status is WorkItemStatus.QUEUED and item.id.startswith(parts[2])
            ]
            if len(queued) != 1:
                raise ValueError("queued work item id is missing or ambiguous")
            item_id = queued[0].id
        context.agent.store.update_work_item(item_id, WorkItemStatus.CANCELLED, error="cancelled before start")
        context.console.print(f"[warning]Cancelled queued work:[/] {item_id[:12]}")
        return
    if len(parts) == 4 and parts[1] == "move" and parts[3].isdigit():
        index = _pending_index(pending, parts[2])
        item = pending.pop(index)
        position = max(0, min(len(pending), int(parts[3]) - 1))
        pending.insert(position, item)
        context.agent.store.reorder_work_item(item[0], position)
        context.console.print(f"[success]Moved queued work to position {position + 1}.[/]")
        return
    raise ValueError("usage: /queue | /queue cancel <id> | /queue move <id> <position>")


def _retry_command(
    context: CliContext,
    prefix: str,
    pending: list[tuple[str, str, str | None]],
    wake: asyncio.Event,
) -> None:
    rows = context.agent.store._connection.execute(
        "SELECT id,question,status,work_item_id FROM runs WHERE session_id=? AND substr(id,1,?)=? ORDER BY started_at DESC LIMIT 2",
        (context.agent.session_id, len(prefix), prefix),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("run id is missing or ambiguous")
    row = rows[0]
    if row["status"] not in {"failed", "cancelled", "interrupted"}:
        raise ValueError(f"run is not retryable: {row['status']}")
    if context.agent.store.last_stable_step(str(row["id"])) is None:
        raise ValueError("run has no stable checkpoint")
    item = context.agent.enqueue(str(row["question"]), parent_work_item_id=row["work_item_id"])
    pending.append((item.id, item.question, str(row["id"])))
    wake.set()
    context.console.print(f"[waiting]Retry queued:[/] {item.question} [text.muted]({item.id[:8]})[/]")


def _pending_index(pending: list[tuple[str, str, str | None]], prefix: str) -> int:
    matches = [index for index, item in enumerate(pending) if item[0].startswith(prefix)]
    if len(matches) != 1:
        raise ValueError("queued work item id is missing or ambiguous")
    return matches[0]
