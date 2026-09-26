"""Versioned in-process notifications; durable mailbox rows remain authoritative."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MailboxSnapshot:
    revision: int = 0
    actionable_revision: int = 0
    closed: bool = False
    generation: int = 0


@dataclass
class _MailboxState:
    generation: int
    revision: int = 0
    actionable_revision: int = 0
    closed: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    consumed_revision: int = 0

    def snapshot(self) -> MailboxSnapshot:
        return MailboxSnapshot(
            self.revision, self.actionable_revision, self.closed, self.generation
        )


class MailboxWakeupRegistry:
    """Notifications are versioned and never consumed by another subscriber."""

    def __init__(self) -> None:
        self._states: dict[str, _MailboxState] = {}
        self._generation = 0

    async def register(self, key: str) -> MailboxSnapshot:
        state = self._states.get(key)
        if state is None or state.closed:
            self._generation += 1
            state = _MailboxState(self._generation)
            self._states[key] = state
        return state.snapshot()

    async def snapshot(self, key: str) -> MailboxSnapshot | None:
        state = self._states.get(key)
        return state.snapshot() if state else None

    async def close(self, key: str, expected_revision: int | None = None) -> bool:
        state = self._states.get(key)
        if state is None:
            return True
        async with state.condition:
            if expected_revision is not None and state.revision != expected_revision:
                return False
            state.closed = True
            state.condition.notify_all()
            return True

    async def unregister(self, key: str) -> None:
        await self.close(key)
        self._states.pop(key, None)

    async def notify(self, key: str, actionable: bool = True) -> bool:
        state = self._states.get(key)
        if state is None:
            return False
        async with state.condition:
            if state.closed:
                return False
            state.revision += 1
            if actionable:
                state.actionable_revision += 1
            state.condition.notify_all()
            return True

    async def wait_since(
        self,
        key: str,
        previous: MailboxSnapshot,
        timeout: float | None = None,
        *,
        actionable_only: bool = False,
    ) -> MailboxSnapshot | None:
        state = self._states.get(key)
        if state is None:
            return None

        def changed() -> bool:
            return (
                state.closed
                or state.generation != previous.generation
                or (
                    state.actionable_revision != previous.actionable_revision
                    if actionable_only
                    else state.revision != previous.revision
                )
            )

        async with state.condition:
            if not changed():
                if timeout is not None and timeout <= 0:
                    return None
                try:
                    async with asyncio.timeout(timeout):
                        await state.condition.wait_for(changed)
                except TimeoutError:
                    return None
            return state.snapshot()

    async def wait(self, key: str, timeout: float | None = None) -> bool:
        """Compatibility single-consumer API; production uses wait_since."""
        state = self._states.get(key)
        if state is None:
            return False
        previous = MailboxSnapshot(
            revision=state.consumed_revision, generation=state.generation
        )
        current = await self.wait_since(key, previous, timeout)
        if current is None or current.closed:
            return False
        state.consumed_revision = current.revision
        return True
