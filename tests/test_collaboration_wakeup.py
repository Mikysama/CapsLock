import asyncio

from capslock.collaboration.wakeup import MailboxWakeupRegistry


def test_mailbox_wakeup_is_scoped_to_registered_task() -> None:
    asyncio.run(_test_mailbox_wakeup_is_scoped_to_registered_task())


async def _test_mailbox_wakeup_is_scoped_to_registered_task() -> None:
    registry = MailboxWakeupRegistry()
    await registry.register("task-a")
    await registry.register("task-b")

    assert await registry.notify("task-a") is True
    assert await registry.wait("task-a", timeout=0) is True
    assert await registry.wait("task-b", timeout=0) is False
    assert await registry.notify("missing") is False


def test_mailbox_wakeup_is_level_triggered_until_consumed() -> None:
    asyncio.run(_test_mailbox_wakeup_is_level_triggered_until_consumed())


async def _test_mailbox_wakeup_is_level_triggered_until_consumed() -> None:
    registry = MailboxWakeupRegistry()
    await registry.register("task")
    await registry.notify("task")

    assert await registry.wait("task", timeout=0) is True
    assert await registry.wait("task", timeout=0) is False


def test_wait_timeout_does_not_leave_a_stale_notification() -> None:
    asyncio.run(_test_wait_timeout_does_not_leave_a_stale_notification())


async def _test_wait_timeout_does_not_leave_a_stale_notification() -> None:
    registry = MailboxWakeupRegistry()
    await registry.register("task")

    assert await registry.wait("task", timeout=0) is False
    await asyncio.sleep(0)
    assert await registry.wait("task", timeout=0) is False
