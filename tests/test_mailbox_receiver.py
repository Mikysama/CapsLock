"""Runtime mailbox delivery boundaries independent of model scheduling."""

import asyncio
from types import SimpleNamespace

import pytest


class ReceiptStore:
    def __init__(self):
        self.delivered = set()
        self.commits = []

    async def delivered_ids(self, session_id):
        return set(self.delivered)

    async def historical_pending(self, session_id):
        return []

    async def clear_historical(self, session_id, message_ids):
        pass

    async def commit_delivery(self, session_id, run_id, message_ids, checkpoint):
        self.commits.append(checkpoint)
        self.delivered.update(message_ids)


class Source:
    def __init__(self):
        self.messages = []
        self.reads = 0
        self.acks = []

    async def read_mailbox_batch(self, address, **kwargs):
        self.reads += 1
        return {"messages": list(self.messages), "has_more": False}

    async def acknowledge_mailbox_batch(self, ids, *, address):
        self.acks.extend(ids)
        self.messages = [m for m in self.messages if m["id"] not in ids]


def mail(identifier="m1", kind="question"):
    return {
        "id": identifier,
        "task_id": "task",
        "message_kind": kind,
        "sender": "child",
        "sender_address": "worker:one",
        "recipient_address": "session:parent",
        "payload": {"text": "help"},
    }


async def make_receiver():
    from capslock.collaboration.wakeup import MailboxWakeupRegistry
    from capslock.runtime.mailbox import MailboxReceiver

    source = Source()
    store = ReceiptStore()
    registry = MailboxWakeupRegistry()
    receiver = MailboxReceiver(
        source=source,
        receipts=store,
        registry=registry,
        address="session:parent",
        session_id="parent",
        run_id="run",
        task_ids=["task"],
    )
    return receiver, source, store, registry


def test_empty_mailbox_does_not_query_each_model_round():
    async def scenario():
        receiver, source, _, _ = await make_receiver()
        await receiver.start()
        try:
            for _ in range(100):
                assert await receiver.poll([]) == 0
            assert source.reads == 1
        finally:
            await receiver.close()

    asyncio.run(scenario())


def test_checkpoint_precedes_source_ack_and_context_change():
    async def scenario():
        receiver, source, store, registry = await make_receiver()
        source.messages = [mail()]
        original_ack = source.acknowledge_mailbox_batch

        async def checked_ack(ids, *, address):
            assert "m1" in store.delivered
            await original_ack(ids, address=address)

        source.acknowledge_mailbox_batch = checked_ack
        await receiver.start()
        try:
            assert source.acks == []
            messages = [{"role": "user", "content": "real objective"}]
            assert await receiver.poll(messages) == 1
            assert source.acks == ["m1"]
            assert store.commits[-1]["messages"] == messages
            assert "Untrusted" in messages[-1]["content"]
            assert "m1" in messages[-1]["content"]
            assert await receiver.poll(messages) == 0
        finally:
            await receiver.close()

    asyncio.run(scenario())


def test_checkpoint_failure_does_not_change_live_context_or_ack_source():
    async def scenario():
        receiver, source, store, _ = await make_receiver()
        source.messages = [mail()]

        async def fail(*args, **kwargs):
            raise OSError("disk full")

        store.commit_delivery = fail
        await receiver.start()
        try:
            messages = [{"role": "user", "content": "goal"}]
            with pytest.raises(OSError, match="disk full"):
                await receiver.poll(messages)
            assert len(messages) == 1
            assert source.acks == []
            assert source.messages[0]["id"] == "m1"
        finally:
            await receiver.close()

    asyncio.run(scenario())


def test_manual_delivery_is_not_also_injected_automatically():
    async def scenario():
        receiver, source, store, _ = await make_receiver()
        source.messages = [mail()]
        await receiver.start()
        try:
            values = await receiver.receive_messages()
            assert [m["id"] for m in values] == ["m1"]
            messages = [{"role": "tool", "tool_call_id": "call", "content": "help"}]
            await receiver.confirm_manual(messages)
            assert await receiver.poll(messages) == 0
            assert len(messages) == 1
            assert store.delivered == {"m1"}
        finally:
            await receiver.close()

    asyncio.run(scenario())


def test_context_budget_defers_whole_message_without_acknowledging_processing():
    async def scenario():
        receiver, source, store, _ = await make_receiver()
        source.messages = [mail()]
        receiver.context_budget = SimpleNamespace(
            hard_limit_tokens=100,
            safety_margin_tokens=10,
            estimate=lambda messages: 1000 if len(messages) > 1 else 50,
        )
        await receiver.start()
        try:
            messages = [{"role": "user", "content": "goal"}]
            assert await receiver.poll(messages) == 0
            assert len(messages) == 1
            assert source.acks == []
            assert receiver.pending_count == 1
        finally:
            await receiver.close()

    asyncio.run(scenario())


def test_receiving_progress_does_not_hide_a_later_question():
    async def scenario():
        receiver, source, store, registry = await make_receiver()
        source.messages = [mail("progress", "progress")]
        await receiver.start()
        try:
            source.messages = [mail("question", "question")]
            await registry.notify(receiver.address, actionable=True)
            messages = []
            assert await receiver.poll(messages, actionable_only=True) == 1
            assert "question" in messages[0]["content"]
            assert any(m["id"] == "progress" for m in receiver._pending)
        finally:
            await receiver.close()

    asyncio.run(scenario())


def test_reconcile_failure_is_reported_at_next_safe_boundary():
    async def scenario():
        receiver, source, _, _ = await make_receiver()
        await receiver.start()
        try:
            receiver._background_error = OSError("reconcile failed")
            with pytest.raises(OSError, match="reconcile failed"):
                await receiver.poll([])
        finally:
            await receiver.close()

    asyncio.run(scenario())
