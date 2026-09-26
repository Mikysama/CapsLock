"""UI-independent foreground run queue and authorization lifecycle."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..domain import AgentEvent, MemoryScope, RunMode, WorkItemStatus
from ..runtime import RunRequest


class ControllerEventKind(StrEnum):
    QUEUED = "queued"
    DEQUEUED = "dequeued"
    STARTED = "started"
    RUN_EVENT = "run_event"
    CANCELLED = "cancelled"
    FAILED = "failed"
    FINISHED = "finished"


@dataclass(frozen=True)
class ControllerEvent:
    kind: ControllerEventKind
    work_item_id: str | None = None
    event: AgentEvent | None = None
    error: str | None = None


@dataclass(frozen=True)
class RecalledWorkItem:
    id: str
    question: str
    queue_index: int
    position: int | None = None


@dataclass(frozen=True)
class _PendingRequest:
    item_id: str
    question: str
    resume_from: str | None = None


ControllerConsumer = Callable[[ControllerEvent], Awaitable[None]]


class AuthorizerBindings:
    """Install and reliably remove model/action authorizers."""

    def __init__(
        self,
        session: Any,
        *,
        action_authorizer=None,
        budget_authorizer=None,
    ) -> None:
        self.session = session
        self.action_authorizer = action_authorizer
        self.budget_authorizer = budget_authorizer

    async def __aenter__(self) -> "AuthorizerBindings":
        budget_setter = getattr(self.session.chat_model, "set_budget_authorizer", None)
        if callable(budget_setter):
            budget_setter(self.budget_authorizer)
        action_setter = getattr(self.session, "set_action_authorizer", None)
        if callable(action_setter):
            action_setter(self.action_authorizer)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        budget_setter = getattr(self.session.chat_model, "set_budget_authorizer", None)
        if callable(budget_setter):
            budget_setter(None)
        action_setter = getattr(self.session, "set_action_authorizer", None)
        if callable(action_setter):
            action_setter(None)


class ForegroundRunController:
    def __init__(
        self,
        session: Any,
        *,
        consumer: ControllerConsumer,
        authorize_limit=None,
        delete_empty_session: bool = True,
    ) -> None:
        self.delete_empty_session = delete_empty_session
        self.session = session
        self.consumer = consumer
        self.authorize_limit = authorize_limit
        self.queue: deque[_PendingRequest] = deque()
        self._queue_changed = asyncio.Condition()
        self._shutdown_requested = False
        self.worker_task: asyncio.Task[None] | None = None
        self.active_task: asyncio.Task[None] | None = None
        self.active_item_id: str | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    async def start(self) -> None:
        if self.worker_task is None:
            self.worker_task = asyncio.create_task(
                self._worker(), name="capslock-foreground-worker"
            )

    async def submit(self, question: str):
        item = await self.session.enqueue(question)
        await self.enqueue_item(item.id, item.question)
        return item

    async def enqueue_item(
        self,
        item_id: str,
        question: str,
        resume_from: str | None = None,
        *,
        queue_index: int | None = None,
    ) -> None:
        if self._shutdown_requested:
            raise RuntimeError("foreground controller is closed")
        await self.start()
        request = _PendingRequest(item_id, question, resume_from)
        async with self._queue_changed:
            if self.contains(item_id):
                return
            self._idle.clear()
            if queue_index is None or queue_index >= len(self.queue):
                self.queue.append(request)
            else:
                self.queue.insert(max(0, queue_index), request)
            self._queue_changed.notify()
        await self.consumer(ControllerEvent(ControllerEventKind.QUEUED, item_id))

    async def recall_latest(self) -> RecalledWorkItem | None:
        """Cancel and remove the newest work item that has not started."""

        async with self._queue_changed:
            if not self.queue:
                return None
            queue_index = len(self.queue) - 1
            request = self.queue.pop()
            try:
                cancelled = await self.session.cancel_queued_work_item(request.item_id)
            except BaseException:
                self.queue.append(request)
                self._queue_changed.notify()
                raise
        await self.consumer(
            ControllerEvent(ControllerEventKind.DEQUEUED, request.item_id)
        )
        return RecalledWorkItem(
            request.item_id,
            request.question,
            queue_index,
            getattr(cancelled, "position", None),
        )

    async def submit_recalled(self, recalled: RecalledWorkItem, question: str):
        item = await self.session.enqueue(question)
        reorder = getattr(self.session, "reorder_queued_work_item", None)
        if recalled.position is not None and callable(reorder):
            await reorder(item.id, recalled.position)
        await self.enqueue_item(
            item.id,
            item.question,
            queue_index=recalled.queue_index,
        )
        return item

    async def retry(self, prefix: str):
        run = await self.session.retryable_run(prefix)
        item = await self.session.enqueue(
            run.question, parent_work_item_id=run.work_item_id
        )
        await self.enqueue_item(item.id, item.question, run.id)
        return item, run

    async def start_queued(self, prefix: str):
        item = await self.session.queued_work_item(prefix)
        if item.status is not WorkItemStatus.QUEUED:
            raise ValueError("only queued work can be started")
        await self.enqueue_item(item.id, item.question)
        return item

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def contains(self, item_id: str) -> bool:
        return self.active_item_id == item_id or any(
            item.item_id == item_id for item in self.queue
        )

    async def wait_idle(self) -> None:
        await self._idle.wait()

    async def wait_item(self, item_id: str) -> None:
        async with self._queue_changed:
            await self._queue_changed.wait_for(lambda: not self.contains(item_id))

    async def cancel(self, item_id: str | None = None) -> bool:
        async with self._queue_changed:
            pending = next(
                (item for item in self.queue if item.item_id == item_id), None
            )
            if pending is not None:
                await self.session.cancel_queued_work_item(pending.item_id)
                self.queue.remove(pending)
                self._queue_changed.notify_all()
                if not self.queue and self.active_item_id is None:
                    self._idle.set()
                return True
        if item_id is not None and item_id != self.active_item_id:
            return False
        if self.active_task is None or self.active_task.done():
            return False
        self.active_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.active_task
        return True

    async def shutdown(self) -> None:
        async with self._queue_changed:
            self._shutdown_requested = True
            pending = list(self.queue)
            self.queue.clear()
            self._queue_changed.notify_all()
        for item in pending:
            await self.session.cancel_queued_work_item(item.item_id)
        if self.active_task is not None and not self.active_task.done():
            self.active_task.cancel()
        if self.worker_task is not None and not self.worker_task.done():
            with suppress(asyncio.CancelledError):
                await self.worker_task
        self._idle.set()
        if self.delete_empty_session:
            await self._delete_empty_session()

    async def _worker(self) -> None:
        while True:
            async with self._queue_changed:
                await self._queue_changed.wait_for(
                    lambda: bool(self.queue) or self._shutdown_requested
                )
                if not self.queue:
                    return
                request = self.queue.popleft()
                self.active_item_id = request.item_id
            item_id = request.item_id
            question = request.question
            resume_from = request.resume_from
            try:
                await self.consumer(
                    ControllerEvent(ControllerEventKind.STARTED, item_id)
                )
                if self._shutdown_requested:
                    await self.session.cancel_queued_work_item(item_id)
                else:
                    self.active_task = asyncio.create_task(
                        self._run(item_id, question, resume_from)
                    )
                    await self.active_task
            except asyncio.CancelledError:
                await self.session.cancel_queued_work_item(item_id)
                await self.consumer(
                    ControllerEvent(ControllerEventKind.CANCELLED, item_id)
                )
            except Exception as exc:
                await self.consumer(
                    ControllerEvent(
                        ControllerEventKind.FAILED,
                        item_id,
                        error=str(exc) or type(exc).__name__,
                    )
                )
            finally:
                self.active_task = None
                self.active_item_id = None
                await self.consumer(
                    ControllerEvent(ControllerEventKind.FINISHED, item_id)
                )
                async with self._queue_changed:
                    if not self.queue:
                        self._idle.set()
                    self._queue_changed.notify_all()

    async def _run(self, item_id: str, question: str, resume_from: str | None) -> None:
        final_run_id: str | None = None
        stream = self.session.run_stream(
            RunRequest(
                question=question,
                work_item_id=item_id,
                resume_from_run_id=resume_from,
                mode=RunMode.INTERACTIVE,
                authorize_limit=self.authorize_limit,
            )
        )
        async with aclosing(stream):
            async for event in stream:
                final_run_id = event.run_id
                await self.consumer(
                    ControllerEvent(ControllerEventKind.RUN_EVENT, item_id, event=event)
                )
        if (
            not self._shutdown_requested
            and final_run_id is not None
            and hasattr(self.session, "implementation_for_planning_run")
        ):
            implementation = await self.session.implementation_for_planning_run(
                final_run_id
            )
            if implementation is not None:
                await self.enqueue_item(implementation.id, implementation.question)

    async def _delete_empty_session(self) -> None:
        memory = self.session.memory
        if memory is not None:
            try:
                if await memory.list(
                    scope=MemoryScope.SESSION,
                    include_inactive=True,
                    limit=1,
                ):
                    return
            except Exception:
                return
        await self.session.delete_if_empty()
