"""Durable mailbox bounds, receipts, and checkpoint crash boundaries."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from capslock.collaboration import AgentTaskContract, MailboxMessageKind
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import workspace_run


def test_mailbox_batch_is_bounded_fair_and_empty_read_only(tmp_path: Path) -> None:
    async def scenario():
        repos = await WorkspaceRepositories.open(tmp_path / "db", workspace=tmp_path)
        try:
            session, prepared = await workspace_run(repos)
            task = AgentTaskContract.create(prepared.run.id, "inspect")
            await repos.collaboration.create_task(task)
            for index in range(40):
                await repos.collaboration.send_mailbox(
                    task_id=task.task_id,
                    parent_run_id=prepared.run.id,
                    sender="child",
                    recipient="parent",
                    kind=MailboxMessageKind.QUESTION
                    if index < 30
                    else MailboxMessageKind.PROGRESS,
                    payload={"text": str(index)},
                )
            address = f"session:{session.id}"
            before = repos.database._commit_samples
            batch = await repos.collaboration.read_mailbox_batch(address)
            assert len(batch["messages"]) == 32
            assert sum(m["message_kind"] == "question" for m in batch["messages"]) == 24
            assert batch["has_more"] is True
            assert repos.database._commit_samples == before
            ids = [m["id"] for m in batch["messages"]]
            await repos.collaboration.acknowledge_mailbox_batch(ids, address=address)
            await repos.collaboration.acknowledge_mailbox_batch(ids, address=address)
            with pytest.raises(ValueError, match="recipient"):
                await repos.collaboration.acknowledge_mailbox_batch(
                    ids, address="session:other"
                )
            tail = await repos.collaboration.read_mailbox_batch(address, max_bytes=1)
            assert tail == {"messages": [], "has_more": True}
            before = repos.database._commit_samples
            assert await repos.collaboration.read_mailbox_batch("session:absent") == {
                "messages": [],
                "has_more": False,
            }
            assert repos.database._commit_samples == before
        finally:
            await repos.close()

    asyncio.run(scenario())


def test_schema_21_upgrade_preserves_rows_and_creates_one_backup(
    tmp_path: Path,
) -> None:
    from capslock.storage.schema import WORKSPACE_APPLICATION_ID, WORKSPACE_SCHEMA

    source_schema = WORKSPACE_SCHEMA.replace("'approval','mailbox'", "'approval'")
    start = source_schema.index("CREATE INDEX idx_agent_mailbox_address_pending")
    end = source_schema.index("CREATE TABLE agent_outputs", start)
    source_schema = source_schema[:start] + source_schema[end:]
    source_schema = source_schema.replace(
        "  delivery_suspended INTEGER NOT NULL DEFAULT 0 CHECK(delivery_suspended IN (0,1)),\n",
        "",
    )
    for column in ("sender_address", "recipient_address", "reply_to_message_id"):
        source_schema = source_schema.replace(f"  {column} TEXT,\n", "")
    start = source_schema.index("CREATE TABLE agent_mailbox (")
    source_schema = source_schema[:start] + source_schema[start:].replace(
        "task_id TEXT REFERENCES agent_tasks",
        "task_id TEXT NOT NULL REFERENCES agent_tasks",
        1,
    )
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(source_schema)
    connection.execute(f"PRAGMA application_id={WORKSPACE_APPLICATION_ID}")
    connection.execute("PRAGMA user_version=21")
    connection.commit()
    connection.close()

    async def scenario():
        for _ in range(2):
            repos = await WorkspaceRepositories.open(path, workspace=tmp_path)
            try:
                assert (await repos.database.fetch_one("PRAGMA user_version"))[0] == 22
                assert not await repos.database.fetch_all("PRAGMA foreign_key_check")
                assert await repos.database.fetch_one(
                    "SELECT name FROM sqlite_master WHERE name='mailbox_deliveries'"
                )
            finally:
                await repos.close()

    asyncio.run(scenario())
    backups = list((tmp_path / "backups").glob("capslock-v21-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 21


def test_durable_receipts_deduplicate_and_checkpoint_atomically(tmp_path: Path) -> None:
    async def scenario():
        repos = await WorkspaceRepositories.open(tmp_path / "db", workspace=tmp_path)
        try:
            from capslock.storage.repositories.mailbox import MailboxDeliveryRepository

            session, prepared = await workspace_run(repos)
            task = AgentTaskContract.create(prepared.run.id, "inspect")
            await repos.collaboration.create_task(task)
            message = await repos.collaboration.send_mailbox(
                task_id=task.task_id,
                parent_run_id=prepared.run.id,
                sender="child",
                recipient="parent",
                kind=MailboxMessageKind.QUESTION,
                payload={"text": "which file?"},
            )
            receiver = MailboxDeliveryRepository(repos.database)
            await receiver.accept(session.id, prepared.run.id, [message])
            await receiver.accept(session.id, prepared.run.id, [message])
            pending = await receiver.pending(session.id, prepared.run.id)
            assert [m["id"] for m in pending["messages"]] == [message["id"]]
            await repos.collaboration.acknowledge_mailbox_batch(
                [message["id"]], address=f"session:{session.id}"
            )
            checkpoint = {
                "messages": [{"role": "user", "content": "which file?"}],
                "mailbox_watermark": 1,
            }
            with pytest.raises(ValueError):
                await receiver.commit_delivery(
                    session.id, prepared.run.id, ["absent"], checkpoint
                )
            assert (await receiver.pending(session.id, prepared.run.id))["messages"]
            await receiver.commit_delivery(
                session.id, prepared.run.id, [message["id"]], checkpoint
            )
            assert not (await receiver.pending(session.id, prepared.run.id))["messages"]
            stable = await repos.run_journal.last_stable_step(prepared.run.id)
            assert stable.checkpoint == checkpoint
            await receiver.accept(session.id, prepared.run.id, [message])
            assert not (await receiver.pending(session.id, prepared.run.id))["messages"]
            row = await repos.database.fetch_one(
                "SELECT count(*) FROM mailbox_deliveries"
            )
            assert row[0] == 1
        finally:
            await repos.close()

    asyncio.run(scenario())


def test_receiver_crash_boundaries_do_not_lose_or_duplicate_messages(
    tmp_path: Path,
) -> None:
    async def scenario():
        from capslock.storage.repositories.mailbox import MailboxDeliveryRepository

        source = await WorkspaceRepositories.open(
            tmp_path / "source", workspace=tmp_path
        )
        target = await WorkspaceRepositories.open(
            tmp_path / "target", workspace=tmp_path
        )
        try:
            owner, parent = await workspace_run(source)
            recipient, child = await workspace_run(target)
            task = AgentTaskContract.create(parent.run.id, "inspect")
            await source.collaboration.create_task(task)
            message = await source.collaboration.send_mailbox(
                task_id=task.task_id,
                parent_run_id=parent.run.id,
                sender="parent",
                recipient="child",
                kind=MailboxMessageKind.INSTRUCTION,
                payload={"text": "check"},
            )
            address = f"task:{task.task_id}"
            receiver = MailboxDeliveryRepository(target.database)
            # Crash after receiving locally, before acknowledging the source.
            await receiver.accept(recipient.id, child.run.id, [message])
            await target.close()
            target = await WorkspaceRepositories.open(
                tmp_path / "target", workspace=tmp_path
            )
            receiver = MailboxDeliveryRepository(target.database)
            reread = (await source.collaboration.read_mailbox_batch(address))[
                "messages"
            ]
            await receiver.accept(recipient.id, child.run.id, reread)
            await source.collaboration.acknowledge_mailbox_batch(
                [message["id"]], address=address
            )
            # Crash after source ACK, before checkpoint injection.
            await target.close()
            target = await WorkspaceRepositories.open(
                tmp_path / "target", workspace=tmp_path
            )
            receiver = MailboxDeliveryRepository(target.database)
            assert not (await source.collaboration.read_mailbox_batch(address))[
                "messages"
            ]
            assert (
                len((await receiver.pending(recipient.id, child.run.id))["messages"])
                == 1
            )
            await receiver.commit_delivery(
                recipient.id,
                child.run.id,
                [message["id"]],
                {"messages": [], "mailbox_watermark": 1},
            )
            await target.close()
            target = await WorkspaceRepositories.open(
                tmp_path / "target", workspace=tmp_path
            )
            receiver = MailboxDeliveryRepository(target.database)
            assert not (await receiver.pending(recipient.id, child.run.id))["messages"]
            await receiver.accept(recipient.id, child.run.id, [message])
            assert not (await receiver.pending(recipient.id, child.run.id))["messages"]
        finally:
            await target.close()
            await source.close()

    asyncio.run(scenario())


def test_idle_worker_mail_and_archival_activation_are_explicit(tmp_path: Path) -> None:
    async def scenario():
        repos = await WorkspaceRepositories.open(tmp_path / "db", workspace=tmp_path)
        try:
            owner, parent = await workspace_run(repos)
            team = await repos.collaboration.ensure_default_team(owner.id)
            worker = await repos.collaboration.create_worker(team, "reader")
            address = f"worker:{worker['id']}"
            message = await repos.collaboration.send_mailbox(
                task_id=None,
                parent_run_id=parent.run.id,
                sender="parent",
                recipient="child",
                kind=MailboxMessageKind.INSTRUCTION,
                payload={"text": "read"},
                worker_id=worker["id"],
                owner_session_id=owner.id,
            )
            assert message["task_id"] is None
            await repos.database.execute(
                "UPDATE agent_mailbox SET delivery_suspended=1 WHERE id=?",
                (message["id"],),
            )
            assert not (await repos.collaboration.read_mailbox_batch(address))[
                "messages"
            ]
            await repos.collaboration.activate_suspended(address, [])
            assert [
                m["id"]
                for m in (await repos.collaboration.read_mailbox_batch(address))[
                    "messages"
                ]
            ] == [message["id"]]
        finally:
            await repos.close()

    asyncio.run(scenario())
