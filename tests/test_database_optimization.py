"""Database capacity, retention, and compaction regression tests."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capslock.configuration import StorageSettings
from capslock.domain import AgentEventKind, RunStepKind, RunStepStatus
from capslock.layout import ProjectLayout, UserLayout
from capslock.lifecycle import LifecycleService
from capslock.runtime.events import RunEventBus
from capslock.storage.async_database import MemoryDatabase, WorkspaceDatabase
from capslock.storage.repositories import WorkspaceRepositories
from capslock.storage.retention import run_retention_maintenance
from capslock.storage.schema import MEMORY_SCHEMA, WORKSPACE_SCHEMA
from tests.helpers import workspace_run


def test_final_schema_has_77_logical_tables_and_no_duplicate_indexes() -> None:
    definitions = re.findall(
        r"(?m)^CREATE (?:VIRTUAL )?TABLE", WORKSPACE_SCHEMA + MEMORY_SCHEMA
    )
    assert len(definitions) == 77
    for schema in (WORKSPACE_SCHEMA, MEMORY_SCHEMA):
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(schema)
            for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ):
                seen: set[tuple[str, ...]] = set()
                for index in connection.execute(f"PRAGMA index_list({table!r})"):
                    columns = tuple(
                        str(row[2])
                        for row in connection.execute(
                            f"PRAGMA index_info({str(index[1])!r})"
                        )
                    )
                    assert columns not in seen
                    seen.add(columns)
        finally:
            connection.close()


def test_checkpoint_pruning_preserves_resume_and_current_pause(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            _session, prepared = await workspace_run(repositories)
            first = await repositories.run_journal.create_step(
                prepared.run.id, RunStepKind.MODEL
            )
            await repositories.run_journal.finish_step(
                first.id,
                status=RunStepStatus.COMPLETED,
                checkpoint={"messages": ["first"]},
            )
            await repositories.database.execute(
                "UPDATE runs SET resume_from_step_id=? WHERE id=?",
                (first.id, prepared.run.id),
            )
            second = await repositories.run_journal.create_step(
                prepared.run.id, RunStepKind.MODEL
            )
            await repositories.run_journal.finish_step(
                second.id,
                status=RunStepStatus.COMPLETED,
                checkpoint={"messages": ["second"]},
            )
            paused = await repositories.run_journal.create_step(
                prepared.run.id, RunStepKind.TOOL
            )
            await repositories.run_journal.pause_step(
                paused.id, kind="approval", checkpoint={"messages": ["paused"]}
            )
            rows = await repositories.database.fetch_all(
                "SELECT id,checkpoint_json FROM run_steps WHERE run_id=? ORDER BY ordinal",
                (prepared.run.id,),
            )
            assert [row["checkpoint_json"] is not None for row in rows] == [
                True,
                False,
                True,
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_durable_delta_coalescing_respects_event_boundaries() -> None:
    class Journal:
        def __init__(self) -> None:
            self.events = []

        async def event_state(self, run_id):
            return 0, "session", "work", "trace"

        async def append_prepared_events(self, events):
            self.events.extend(events)

    async def scenario() -> None:
        journal = Journal()

        async def consume(_event) -> None:
            return None

        bus = RunEventBus(
            run_id="run",
            journal=journal,
            consumer=consume,
            diagnostic=lambda *args, **values: None,
        )
        for _ in range(100):
            await bus.emit(AgentEventKind.TEXT_DELTA, {"text": "x"})
        await bus.emit(AgentEventKind.TOOL_RUNNING, {"name": "read_file"})
        for _ in range(3):
            await bus.emit(AgentEventKind.TEXT_DELTA, {"text": "y"})
        await bus.flush()
        assert [event.kind for event in journal.events] == [
            AgentEventKind.TEXT_DELTA,
            AgentEventKind.TOOL_RUNNING,
            AgentEventKind.TEXT_DELTA,
        ]
        assert journal.events[0].sequence == 100
        assert journal.events[0].data["text"] == "x" * 100
        assert journal.events[2].sequence == 104
        await bus.close()

    asyncio.run(scenario())


def test_retention_keeps_latest_empty_recall_and_is_interval_limited(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = await WorkspaceDatabase.open(tmp_path / "workspace.sqlite3")
        memory = await MemoryDatabase.open(tmp_path / "memory.sqlite3")
        current = datetime(2026, 9, 2, tzinfo=UTC)
        older = (current - timedelta(days=60)).isoformat()
        newer = (current - timedelta(days=40)).isoformat()
        try:
            async with memory.transaction() as connection:
                await connection.execute(
                    "INSERT INTO memory_recalls VALUES('old','workspace','session','q',?)",
                    (older,),
                )
                await connection.execute(
                    "INSERT INTO memory_recalls VALUES('latest','workspace','session','q',?)",
                    (newer,),
                )
                await connection.execute(
                    "INSERT INTO memory_recalls VALUES('only','other','empty','q',?)",
                    (older,),
                )
                await connection.execute(
                    "INSERT INTO memory_audit(operation,created_at) VALUES('test',?)",
                    ((current - timedelta(days=200)).isoformat(),),
                )
            removed = await run_retention_maintenance(
                workspace, memory, StorageSettings(), now=current
            )
            assert removed["memory_recalls"] == 1
            assert removed["memory_audit"] == 1
            rows = await memory.fetch_all(
                "SELECT run_id FROM memory_recalls ORDER BY run_id"
            )
            assert [str(row["run_id"]) for row in rows] == ["latest", "only"]
            assert not await run_retention_maintenance(
                workspace, memory, StorageSettings(), now=current
            )
        finally:
            await workspace.close()
            await memory.close()

    asyncio.run(scenario())


def test_explicit_compaction_creates_backup_and_checks_databases(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = ProjectLayout.discover(workspace, user=UserLayout(tmp_path / "home"))

    async def initialize() -> None:
        workspace_database = await WorkspaceDatabase.open(layout.database)
        memory_database = await MemoryDatabase.open(layout.user.memory)
        await workspace_database.close()
        await memory_database.close()

    asyncio.run(initialize())
    report = LifecycleService(layout).compact("all")
    assert Path(report["backup"]).is_file()
    assert {item["scope"] for item in report["databases"]} == {
        "workspace",
        "memory",
    }
    for item in report["databases"]:
        assert item["after_bytes"] <= item["before_bytes"]
        with sqlite3.connect(item["path"]) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
