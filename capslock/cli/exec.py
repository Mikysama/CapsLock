"""Non-interactive v2 execution and JSONL rendering."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TextIO

from ..domain import AgentEvent, AgentEventKind
from ..status import AgentStatus, status_for_event
from .context import CliContext
from .status import AsyncStatusRenderer

EXEC_EVENT_SCHEMA_VERSION = 2
APPROVAL_REQUIRED_EXIT = 3


async def run_exec(
    context: CliContext,
    question: str | None,
    *,
    json_events: bool = False,
    spinner: bool = True,
    quiet: bool = False,
    status_renderer: AsyncStatusRenderer | None = None,
) -> int:
    prompt = question if question is not None else sys.stdin.read()
    if not prompt.strip():
        raise ValueError("exec requires a prompt argument or non-empty stdin")
    exit_code = 0
    terminal_seen = False
    renderer = None
    if not json_events:
        renderer = _ExecStreamRenderer(
            context,
            status_renderer
            or AsyncStatusRenderer(
                enabled=spinner and not quiet,
                output_is_tty=context.console.is_terminal,
            ),
        )
        await renderer.start()
    try:
        async for event in context.agent.ask_stream(prompt):
            if json_events:
                record = {
                    "schema_version": EXEC_EVENT_SCHEMA_VERSION,
                    "sequence": event.sequence,
                    "timestamp": event.timestamp,
                    "session_id": event.session_id,
                    "work_item_id": event.work_item_id,
                    "run_id": event.run_id,
                    "event": event.kind.value,
                    "status": str(event.data.get("status", "running")),
                    "terminal": event.terminal,
                    "data": event.data,
                }
                context.console.file.write(
                    json.dumps(record, ensure_ascii=False) + "\n"
                )
                context.console.file.flush()
            else:
                await renderer.handle(event)
            if event.kind.value == "waiting_approval":
                exit_code = APPROVAL_REQUIRED_EXIT
            elif event.kind.value == "failed":
                exit_code = 1
            elif event.kind.value == "cancelled":
                exit_code = 130
            terminal_seen = terminal_seen or event.terminal
    except asyncio.CancelledError:
        if renderer is not None:
            await renderer.cancel()
        raise
    except Exception:
        if renderer is not None and not terminal_seen:
            await renderer.fail()
        if not terminal_seen:
            raise
    finally:
        if renderer is not None:
            await renderer.close()
    return exit_code


class _ExecStreamRenderer:
    """Coordinate stdout deltas with the stderr status line."""

    def __init__(self, context: CliContext, status: AsyncStatusRenderer) -> None:
        self.context = context
        self.output: TextIO = context.console.file
        self.status = status
        self.streamed = False
        self.line_open = False
        self.terminal = False

    async def start(self) -> None:
        await self.status.start(AgentStatus.THINKING)

    async def handle(self, event: AgentEvent) -> None:
        state, detail = status_for_event(event.kind, event.data)
        if event.kind is AgentEventKind.TEXT_DELTA:
            text = str(event.data.get("text", ""))
            if not text:
                return
            if self.status.running:
                await self.status.stop()
            self.output.write(text)
            self.output.flush()
            self.streamed = True
            self.line_open = not text.endswith("\n")
            return
        if event.kind is AgentEventKind.TOOL_RUNNING:
            self._finish_output_line()
            if self.status.running:
                await self.status.update(state, detail)
            else:
                await self.status.start(state, detail)
            return
        if event.kind in {
            AgentEventKind.QUEUED,
            AgentEventKind.THINKING,
            AgentEventKind.TOOL_COMPLETED,
        }:
            if self.status.running:
                await self.status.update(state, detail)
            elif not self.streamed or event.kind is not AgentEventKind.THINKING:
                await self.status.start(state, detail)
            return
        if not event.terminal:
            return
        self.terminal = True
        await self.status.stop()
        if event.kind is AgentEventKind.COMPLETED:
            if not self.streamed:
                answer = str(event.data.get("answer", ""))
                if answer:
                    self.output.write(answer)
                    self.line_open = not answer.endswith("\n")
            self._finish_output_line()
            self.output.flush()
            await self.status.stop(AgentStatus.DONE)
        elif event.kind is AgentEventKind.CANCELLED:
            await self.status.stop(AgentStatus.CANCELLED)
            self._print_error(event)
        elif event.kind is AgentEventKind.FAILED:
            await self.status.stop(AgentStatus.ERROR)
            self._print_error(event)
        else:
            await self.status.update(AgentStatus.WAITING)

    async def cancel(self) -> None:
        if not self.terminal:
            self._finish_output_line()
            await self.status.stop(AgentStatus.CANCELLED)
            self.terminal = True

    async def fail(self) -> None:
        if not self.terminal:
            self._finish_output_line()
            await self.status.stop(AgentStatus.ERROR)
            self.terminal = True

    async def close(self) -> None:
        await self.status.stop()

    def _finish_output_line(self) -> None:
        if self.line_open:
            self.output.write("\n")
            self.output.flush()
            self.line_open = False

    def _print_error(self, event: AgentEvent) -> None:
        error = event.data.get("error", {})
        message = error.get("message", event.kind.value)
        self.status.stream.write(f"{message}\n")
        self.status.stream.flush()
