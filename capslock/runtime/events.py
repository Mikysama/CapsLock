"""Ordered run event bus with immediate UI and batched durability."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime

from ..domain import AgentEvent, AgentEventKind
from ..security import redact


class RunEventBus:
    def __init__(
        self,
        *,
        run_id: str,
        journal,
        consumer: Callable[[AgentEvent], Awaitable[None]],
        diagnostic: Callable[..., None],
        flush_interval: float = 0.050,
        flush_bytes: int = 4096,
        diagnostic_queue_size: int = 128,
    ) -> None:
        self.run_id = run_id
        self.journal = journal
        self.consumer = consumer
        self.diagnostic = diagnostic
        self.flush_interval = flush_interval
        self.flush_bytes = flush_bytes
        self._lock = asyncio.Lock()
        self._pending: list[AgentEvent] = []
        self._pending_bytes = 0
        self._timer: asyncio.Task[None] | None = None
        self._sequence: int | None = None
        self._session_id = ""
        self._work_item_id = ""
        self._trace_id = ""
        self._failure: BaseException | None = None
        self._diagnostic_queue: asyncio.Queue[AgentEvent] = asyncio.Queue(
            maxsize=diagnostic_queue_size
        )
        self._diagnostic_task: asyncio.Task[None] | None = None
        self._coalesced_text = ""
        self._coalesced_event: AgentEvent | None = None

    async def emit(self, kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
        async with self._lock:
            await self._initialize()
            self._raise_failure()
            assert self._sequence is not None
            self._sequence += 1
            event = AgentEvent(
                self._sequence,
                datetime.now(UTC).isoformat(),
                self._session_id,
                self.run_id,
                self._work_item_id,
                kind,
                redact(data),
                f"evt_{uuid.uuid4().hex}",
                self._trace_id,
            )
            self._pending.append(event)
            self._pending_bytes += len(
                json.dumps(event.data, ensure_ascii=False).encode("utf-8")
            )
            if self._timer is None:
                self._timer = asyncio.create_task(self._flush_after_delay())
            flush_now = self._pending_bytes >= self.flush_bytes
            self._diagnostic(event)
            await self.consumer(event)
        if flush_now:
            await self.flush()
        return event

    async def publish_persisted(self, event: AgentEvent) -> None:
        await self.flush()
        self._diagnostic(event)
        await self.consumer(event)

    async def flush(self) -> None:
        async with self._lock:
            self._raise_failure()
            pending, self._pending = self._pending, []
            self._pending_bytes = 0
            timer, self._timer = self._timer, None
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()
            if not pending:
                return
            try:
                await self.journal.append_prepared_events(pending)
            except BaseException as exc:
                self._failure = exc
                raise

    async def close(self) -> None:
        await self.flush()
        task = self._diagnostic_task
        if task is None:
            return
        try:
            async with asyncio.timeout(0.1):
                await self._diagnostic_queue.join()
        except TimeoutError:
            pass
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._diagnostic_task = None

    async def _initialize(self) -> None:
        if self._sequence is not None:
            return
        state = await self.journal.event_state(self.run_id)
        self._sequence = state[0]
        self._session_id = state[1]
        self._work_item_id = state[2]
        self._trace_id = state[3] or f"trace_{uuid.uuid4().hex}"

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.flush_interval)
            await self.flush()
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            self._failure = exc

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise RuntimeError("durable run event sink failed") from self._failure

    def _diagnostic(self, event: AgentEvent) -> None:
        if self._diagnostic_task is None:
            self._diagnostic_task = asyncio.create_task(
                self._drain_diagnostics(), name="capslock-diagnostic-events"
            )
        try:
            self._diagnostic_queue.put_nowait(event)
        except asyncio.QueueFull:
            if event.kind is AgentEventKind.TEXT_DELTA:
                self._coalesced_text += str(event.data.get("text", ""))
                self._coalesced_event = event

    async def _drain_diagnostics(self) -> None:
        while True:
            event = await self._diagnostic_queue.get()
            try:
                try:
                    self.diagnostic(
                        "workflow_event",
                        run_id=event.run_id,
                        work_item_id=event.work_item_id,
                        event=event.kind.value,
                        event_id=event.event_id,
                        trace_id=event.trace_id,
                        data=event.data,
                    )
                except Exception:
                    pass
            finally:
                self._diagnostic_queue.task_done()
            if self._coalesced_event is not None and not self._diagnostic_queue.full():
                latest = self._coalesced_event
                combined = AgentEvent(
                    latest.sequence,
                    latest.timestamp,
                    latest.session_id,
                    latest.run_id,
                    latest.work_item_id,
                    latest.kind,
                    {"text": self._coalesced_text},
                    latest.event_id,
                    latest.trace_id,
                )
                self._coalesced_text = ""
                self._coalesced_event = None
                self._diagnostic_queue.put_nowait(combined)
