"""Asynchronous one-line status rendering for the non-interactive CLI."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import TextIO

from prompt_toolkit.utils import get_cwidth

from ..status import AgentStatus, SPINNER_FRAMES, STATUS_MESSAGES, status_message

_CLEAR_LINE = "\r\x1b[2K"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"


def dynamic_status_supported(
    stream: TextIO,
    *,
    output_is_tty: bool = True,
    environ: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    is_tty = getattr(stream, "isatty", lambda: False)
    if not output_is_tty or not is_tty():
        return False
    if env.get("TERM", "").casefold() == "dumb":
        return False
    if env.get("CI", "").casefold() in {"1", "true", "yes"}:
        return False
    return env.get("CAPSLOCK_NO_SPINNER", "").casefold() not in {"1", "true", "yes"}


class AsyncStatusRenderer:
    """Own the spinner task and all ANSI writes for a single status line."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        enabled: bool = True,
        output_is_tty: bool = True,
        interval: float = 0.1,
        environ: Mapping[str, str] | None = None,
        width_provider: Callable[[], int] | None = None,
    ) -> None:
        self.stream = stream or sys.stderr
        self.enabled = enabled and dynamic_status_supported(
            self.stream, output_is_tty=output_is_tty, environ=environ
        )
        self.interval = interval
        self.width_provider = width_provider or (
            lambda: shutil.get_terminal_size(fallback=(80, 24)).columns
        )
        self.status = AgentStatus.IDLE
        self.detail: str | None = None
        self._frame = 0
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._result_shown = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self, status: AgentStatus = AgentStatus.THINKING, detail: str | None = None
    ) -> None:
        self.status, self.detail = status, detail
        if not self.enabled:
            return
        if self.running:
            await self._draw()
            return
        self._frame = 0
        self._result_shown = False
        self._write(_HIDE_CURSOR)
        try:
            await self._draw()
            self._task = asyncio.create_task(self._animate())
        except BaseException:
            self._write(_CLEAR_LINE + _SHOW_CURSOR)
            raise

    async def update(self, status: AgentStatus, detail: str | None = None) -> None:
        self.status, self.detail = status, detail
        if self.running:
            await self._draw()

    async def stop(self, result: AgentStatus | None = None) -> None:
        task, self._task = self._task, None
        animation_error: BaseException | None = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                animation_error = exc
        if self.enabled:
            async with self._lock:
                if task is not None:
                    self._write(_CLEAR_LINE + _SHOW_CURSOR)
                if result is not None and not self._result_shown:
                    symbol = {
                        AgentStatus.DONE: "✓",
                        AgentStatus.ERROR: "✗",
                        AgentStatus.CANCELLED: "!",
                    }.get(result)
                    if symbol is not None:
                        self._write(f"{symbol} {STATUS_MESSAGES[result]}\n")
                        self._result_shown = True
        self.status = result or AgentStatus.IDLE
        self.detail = None
        if animation_error is not None:
            raise animation_error

    async def _animate(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self._frame = (self._frame + 1) % len(SPINNER_FRAMES)
            await self._draw()

    async def _draw(self) -> None:
        async with self._lock:
            width = max(1, self.width_provider())
            frame = SPINNER_FRAMES[self._frame]
            text = _fit_width(
                f"{frame} {status_message(self.status, self.detail)}", width
            )
            self._write(_CLEAR_LINE + text)

    def _write(self, value: str) -> None:
        self.stream.write(value)
        self.stream.flush()

    async def __aenter__(self) -> "AsyncStatusRenderer":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        result = (
            AgentStatus.CANCELLED
            if exc_type is not None and issubclass(exc_type, asyncio.CancelledError)
            else AgentStatus.ERROR
            if exc_type is not None
            else None
        )
        with suppress(Exception):
            await self.stop(result)


def _fit_width(value: str, width: int) -> str:
    if get_cwidth(value) <= width:
        return value
    suffix = "..."
    target = max(0, width - get_cwidth(suffix))
    result = ""
    for character in value:
        if get_cwidth(result + character) > target:
            break
        result += character
    return result.rstrip() + suffix if width >= len(suffix) else suffix[:width]
