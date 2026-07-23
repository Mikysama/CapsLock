"""UI-independent foreground run queue and authorization lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..domain import AgentEvent, MemoryScope, RunMode, WorkItemStatus


class ControllerEventKind(StrEnum):
    QUEUED = "queued"
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


ControllerConsumer = Callable[[ControllerEvent], Awaitable[None]]


class AuthorizerBindings:
    """Install and reliably remove model/action authorizers."""

    def __init__(
        self,
        agent: Any,
        *,
        action_authorizer=None,
        budget_authorizer=None,
    ) -> None:
        self.agent = agent
        self.action_authorizer = action_authorizer
        self.budget_authorizer = budget_authorizer

    async def __aenter__(self) -> "AuthorizerBindings":
        budget_setter = getattr(self.agent.chat_model, "set_budget_authorizer", None)
        if callable(budget_setter):
            budget_setter(self.budget_authorizer)
        action_setter = getattr(self.agent, "set_action_authorizer", None)
        if callable(action_setter):
            action_setter(self.action_authorizer)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        budget_setter = getattr(self.agent.chat_model, "set_budget_authorizer", None)
        if callable(budget_setter):
            budget_setter(None)
        action_setter = getattr(self.agent, "set_action_authorizer", None)
        if callable(action_setter):
            action_setter(None)


class ForegroundRunController:
    def __init__(
        self,
        agent: Any,
        *,
        consumer: ControllerConsumer,
        authorize_limit=None,
    ) -> None:
        self.agent = agent
        self.consumer = consumer
        self.authorize_limit = authorize_limit
        self.queue: asyncio.Queue[tuple[str, str, str | None] | None] = asyncio.Queue()
        self.worker_task: asyncio.Task[None] | None = None
        self.active_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.worker_task is None:
            self.worker_task = asyncio.create_task(
                self._worker(), name="capslock-foreground-worker"
            )

    async def submit(self, question: str):
        item = await self.agent.enqueue(question)
        await self.enqueue_item(item.id, item.question)
        return item

    async def enqueue_item(
        self,
        item_id: str,
        question: str,
        resume_from: str | None = None,
    ) -> None:
        await self.start()
        await self.queue.put((item_id, question, resume_from))
        await self.consumer(ControllerEvent(ControllerEventKind.QUEUED, item_id))

    async def retry(self, prefix: str):
        run = await self.agent.repositories.runs.retryable(
            self.agent.session_id, prefix
        )
        item = await self.agent.enqueue(
            run.question, parent_work_item_id=run.work_item_id
        )
        await self.enqueue_item(item.id, item.question, run.id)
        return item, run

    async def start_queued(self, prefix: str):
        item = await self.agent.repositories.work_items.require(prefix)
        if item.session_id != self.agent.session_id:
            raise ValueError("work item does not belong to this session")
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
        await self.queue.put(None)
        if self.active_task is not None and not self.active_task.done():
            self.active_task.cancel()
        if self.worker_task is not None and not self.worker_task.done():
            with suppress(asyncio.CancelledError):
                await self.worker_task
        await self._delete_empty_session()

    async def _worker(self) -> None:
        while True:
            request = await self.queue.get()
            if request is None:
                return
            item_id, question, resume_from = request
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
        async for event in self.agent.ask_stream(
            question,
            work_item_id=item_id,
            resume_from_run_id=resume_from,
            mode=RunMode.INTERACTIVE,
            authorize_limit=self.authorize_limit,
        ):
            await self.consumer(
                ControllerEvent(
                    ControllerEventKind.RUN_EVENT,
                    item_id,
                    event=event,
                )
            )

    async def _delete_empty_session(self) -> None:
        memory = self.agent.memory
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
        await self.agent.repositories.sessions.delete_if_empty(self.agent.session_id)
