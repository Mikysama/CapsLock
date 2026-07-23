"""Serialized run kernel and request boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from ..domain import AgentEvent, BudgetSnapshot, RunLimits, RunMode


@dataclass(frozen=True, kw_only=True)
class RunRequest:
    question: str
    work_item_id: str | None = None
    resume_from_run_id: str | None = None
    mode: RunMode = RunMode.INTERACTIVE
    limits: RunLimits | None = None
    authorize_limit: Callable[[BudgetSnapshot], Awaitable[bool]] | None = None


RunConsumer = Callable[[AgentEvent], Awaitable[None]]
RunExecutor = Callable[..., Awaitable[None]]


class RunEngine:
    """Own session serialization, streaming, cancellation, and dispatch."""

    def __init__(self, executor: RunExecutor) -> None:
        self._executor = executor
        self._lock = asyncio.Lock()
        self._active = 0

    @property
    def active(self) -> bool:
        return self._active > 0 or self._lock.locked()

    async def run_stream(self, request: RunRequest) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()

        async def consume(event: AgentEvent) -> None:
            await queue.put(event)

        async def execute() -> None:
            async with self._lock:
                self._active += 1
                try:
                    await self._executor(
                        request.question,
                        work_item_id=request.work_item_id,
                        resume_from_run_id=request.resume_from_run_id,
                        mode=request.mode,
                        limits=request.limits,
                        authorize_limit=request.authorize_limit,
                        consumer=consume,
                    )
                finally:
                    self._active -= 1
                    await queue.put(None)

        task = asyncio.create_task(execute(), name="capslock-run-engine")
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
            await task
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
