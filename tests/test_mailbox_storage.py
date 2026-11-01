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
                assert (await repos.database.fetch_one("PRAGMA user_version"))[0] == 24
                assert not await repos.database.fetch_all("PRAGMA foreign_key_check")
                assert not await repos.database.fetch_one(
                    "SELECT name FROM sqlite_master WHERE name='mailbox_deliveries'"
                )
            finally:
                await repos.close()

    asyncio.run(scenario())
    backups = list((tmp_path / "backups").glob("capslock-v21-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 21


def test_delivery_ids_are_durable_without_a_second_mailbox_table(tmp_path: Path) -> None:
    async def scenario():
        repos = await WorkspaceRepositories.open(tmp_path / "db", workspace=tmp_path)
        try:
            from capslock.storage.repositories.mailbox import MailboxCheckpointRepository

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
            receiver = MailboxCheckpointRepository(repos.database)
            assert not await receiver.delivered_ids(session.id)
            assert (await repos.collaboration.read_mailbox_batch(
                f"session:{session.id}"
            ))["messages"]
            checkpoint = {
                "messages": [{"role": "user", "content": "which file?"}],
                "mailbox_watermark": message["id"],
            }
            await receiver.commit_delivery(
                session.id, prepared.run.id, [message["id"]], checkpoint
            )
            assert await receiver.delivered_ids(session.id) == {message["id"]}
            stable = await repos.run_journal.last_stable_step(prepared.run.id)
            assert stable.checkpoint == checkpoint
            await repos.collaboration.acknowledge_mailbox_batch(
                [message["id"]], address=f"session:{session.id}"
            )
            assert not (await repos.collaboration.read_mailbox_batch(
                f"session:{session.id}"
            ))["messages"]
            assert not await repos.database.fetch_one(
                "SELECT name FROM sqlite_master WHERE name='mailbox_deliveries'"
            )
        finally:
            await repos.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("source_version", [22, 23])
def test_pending_receipt_upgrade_drops_historical_table(
    tmp_path: Path, source_version: int,
) -> None:
    import hashlib
    import json

    from capslock.collaboration.wakeup import MailboxWakeupRegistry
    from capslock.runtime.mailbox import MailboxReceiver
    from capslock.storage.repositories.mailbox import MailboxCheckpointRepository
    from capslock.storage.upgrades import _HISTORICAL_MAILBOX_DELIVERIES

    path = tmp_path / "legacy.sqlite3"

    async def scenario():
        repos = await WorkspaceRepositories.open(path, workspace=tmp_path)
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
        completed = await repos.collaboration.send_mailbox(
            task_id=task.task_id,
            parent_run_id=prepared.run.id,
            sender="child",
            recipient="parent",
            kind=MailboxMessageKind.PROGRESS,
            payload={"text": "already seen"},
        )
        await repos.collaboration.acknowledge_mailbox_batch(
            [message["id"], completed["id"]], address=f"session:{session.id}"
        )
        await repos.close()
        encoded = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
        with sqlite3.connect(path) as connection:
            connection.executescript(_HISTORICAL_MAILBOX_DELIVERIES)
            connection.execute(
                """INSERT INTO mailbox_deliveries(
                   recipient_session_id,run_id,message_id,task_id,message_kind,
                   envelope_json,envelope_sha256,received_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (session.id, prepared.run.id, message["id"], task.task_id,
                 "question", encoded, hashlib.sha256(encoded.encode()).hexdigest(),
                 message["created_at"]),
            )
            completed_envelope = json.dumps(
                completed, ensure_ascii=False, sort_keys=True, default=str
            )
            if source_version == 22:
                connection.execute(
                    """INSERT INTO mailbox_deliveries(
                       recipient_session_id,run_id,message_id,task_id,message_kind,
                       envelope_json,envelope_sha256,status,received_at,delivered_at)
                       VALUES(?,?,?,?,?,?,?,'delivered',?,?)""",
                    (session.id, prepared.run.id, completed["id"], task.task_id,
                     "progress", completed_envelope,
                     hashlib.sha256(completed_envelope.encode()).hexdigest(),
                     completed["created_at"], completed["created_at"]),
                )
            else:
                connection.execute(
                    """INSERT INTO run_steps(
                       id,run_id,ordinal,kind,status,started_at,finished_at,mailbox_ids_json)
                       VALUES(?,?,999,'mailbox','completed',?,?,?)""",
                    (f"migration_{completed['id']}", prepared.run.id,
                     completed["created_at"], completed["created_at"],
                     json.dumps([completed["id"]])),
                )
            connection.execute(f"PRAGMA user_version={source_version}")
        repos = await WorkspaceRepositories.open(path, workspace=tmp_path)
        try:
            receipts = MailboxCheckpointRepository(repos.database)
            assert await receipts.delivered_ids(session.id) == {completed["id"]}
            assert not await repos.database.fetch_one(
                "SELECT name FROM sqlite_master WHERE name='mailbox_deliveries'"
            )
            assert [item["id"] for item in await receipts.historical_pending(session.id)] == [
                message["id"]
            ]
            receiver = MailboxReceiver(
                source=repos.collaboration,
                receipts=receipts,
                registry=MailboxWakeupRegistry(),
                address=f"session:{session.id}",
                session_id=session.id,
                run_id=prepared.run.id,
                task_ids=[task.task_id],
            )
            await receiver.start()
            try:
                messages = []
                assert await receiver.poll(messages) == 1
                assert "which file?" in messages[0]["content"]
                assert await receipts.delivered_ids(session.id) == {
                    message["id"], completed["id"]
                }
                assert not await receipts.historical_pending(session.id)
                assert not await repos.database.fetch_one(
                    "SELECT name FROM sqlite_master WHERE name='mailbox_deliveries'"
                )
            finally:
                await receiver.close()
        finally:
            await repos.close()

    asyncio.run(scenario())


def test_cross_database_checkpoint_precedes_ack_and_recovers_after_crash(
    tmp_path: Path,
) -> None:
    async def scenario():
        from capslock.storage.repositories.mailbox import MailboxCheckpointRepository

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
            receiver = MailboxCheckpointRepository(target.database)
            # A crash before the checkpoint leaves the original letter queued.
            await target.close()
            target = await WorkspaceRepositories.open(
                tmp_path / "target", workspace=tmp_path
            )
            receiver = MailboxCheckpointRepository(target.database)
            reread = (await source.collaboration.read_mailbox_batch(address))[
                "messages"
            ]
            assert [item["id"] for item in reread] == [message["id"]]
            await receiver.commit_delivery(
                recipient.id, child.run.id, [message["id"]],
                {"messages": [], "mailbox_watermark": message["id"]},
            )
            # A crash before ACK is resolved from the durable checkpoint ID.
            await target.close()
            target = await WorkspaceRepositories.open(
                tmp_path / "target", workspace=tmp_path
            )
            receiver = MailboxCheckpointRepository(target.database)
            assert await receiver.delivered_ids(recipient.id) == {message["id"]}
            assert (await source.collaboration.read_mailbox_batch(address))["messages"]
            await source.collaboration.acknowledge_mailbox_batch(
                [message["id"]], address=address
            )
            assert not (await source.collaboration.read_mailbox_batch(address))["messages"]
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
