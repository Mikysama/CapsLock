import asyncio

from capslock.collaboration.wakeup import MailboxWakeupRegistry


def test_versioned_wait_does_not_consume_other_waiters_notification():
    async def scenario():
        registry = MailboxWakeupRegistry()
        before = await registry.register("session:parent")
        await registry.notify("session:parent", actionable=True)
        first = await registry.wait_since("session:parent", before, timeout=0)
        second = await registry.wait_since("session:parent", before, timeout=0)
        assert first == second
        assert first.revision == 1
        assert first.actionable_revision == 1

    asyncio.run(scenario())


def test_progress_does_not_wake_actionable_wait_and_close_does():
    async def scenario():
        registry = MailboxWakeupRegistry()
        before = await registry.register("worker:child")
        await registry.notify("worker:child", actionable=False)
        assert (
            await registry.wait_since(
                "worker:child", before, timeout=0, actionable_only=True
            )
            is None
        )
        assert not await registry.close("worker:child", expected_revision=0)
        assert await registry.close("worker:child", expected_revision=1)
        closed = await registry.wait_since(
            "worker:child", before, timeout=0, actionable_only=True
        )
        assert closed.closed
        assert not await registry.notify("worker:child")

    asyncio.run(scenario())


def test_unregister_releases_waiters_and_reregister_starts_new_generation():
    async def scenario():
        registry = MailboxWakeupRegistry()
        before = await registry.register("worker:child")
        waiter = asyncio.create_task(registry.wait_since("worker:child", before))
        await asyncio.sleep(0)
        await registry.unregister("worker:child")
        assert (await asyncio.wait_for(waiter, 0.1)).closed
        current = await registry.register("worker:child")
        assert current.generation != before.generation

    asyncio.run(scenario())
