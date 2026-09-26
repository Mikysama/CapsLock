"""Low-latency in-process notifications for collaboration mailboxes."""

from __future__ import annotations

import asyncio


class MailboxWakeupRegistry:
    """Wake registered runtimes after a mailbox row has been committed.

    The registry is only an optimization.  Mailbox state remains durable in the
    repository, so a missed notification is recovered by the next drain.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def register(self, key: str) -> asyncio.Event:
        async with self._lock:
            return self._events.setdefault(key, asyncio.Event())

    async def unregister(self, key: str) -> None:
        async with self._lock:
            self._events.pop(key, None)

    async def notify(self, key: str) -> bool:
        async with self._lock:
            event = self._events.get(key)
            if event is None:
                return False
            event.set()
            return True

    async def wait(self, key: str, timeout: float | None = None) -> bool:
        async with self._lock:
            event = self._events.get(key)
        if event is None:
            return False
        try:
            if event.is_set():
                event.clear()
                return True
            if timeout is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        event.clear()
        return True
