"""UI-independent foreground run queue and authorization lifecycle."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
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
    ) -> None:
        self.session = session
        self.consumer = consumer
        self.authorize_limit = authorize_limit
        self.queue: deque[_PendingRequest] = deque()
        self._queue_changed = asyncio.Condition()
        self._shutdown_requested = False
        self.worker_task: asyncio.Task[None] | None = None
        self.active_task: asyncio.Task[None] | None = None

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
        await self.start()
        request = _PendingRequest(item_id, question, resume_from)
        async with self._queue_changed:
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

    async def cancel(self) -> bool:
        if self.active_task is None or self.active_task.done():
            return False
        self.active_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.active_task
        return True

    async def shutdown(self) -> None:
        async with self._queue_changed:
            self._shutdown_requested = True
            self._queue_changed.notify_all()
        if self.active_task is not None and not self.active_task.done():
            self.active_task.cancel()
        if self.worker_task is not None and not self.worker_task.done():
            with suppress(asyncio.CancelledError):
                await self.worker_task
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
            item_id = request.item_id
            question = request.question
            resume_from = request.resume_from
            await self.consumer(ControllerEvent(ControllerEventKind.STARTED, item_id))
            self.active_task = asyncio.create_task(
                self._run(item_id, question, resume_from)
            )
            try:
                await self.active_task
            except asyncio.CancelledError:
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
                await self.consumer(
                    ControllerEvent(ControllerEventKind.FINISHED, item_id)
                )

    async def _run(self, item_id: str, question: str, resume_from: str | None) -> None:
        final_run_id: str | None = None
        async for event in self.session.run_stream(
            RunRequest(
                question=question,
                work_item_id=item_id,
                resume_from_run_id=resume_from,
                mode=RunMode.INTERACTIVE,
                authorize_limit=self.authorize_limit,
            )
        ):
            final_run_id = event.run_id
            await self.consumer(
                ControllerEvent(
                    ControllerEventKind.RUN_EVENT,
                    item_id,
                    event=event,
                )
            )
        if final_run_id is not None and hasattr(
            self.session, "implementation_for_planning_run"
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
