"""Storage tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from capslock.domain import AgentEventKind, WorkItemStatus
from capslock.layout import LayoutConflict, ProjectLayout, UserLayout
from capslock.session_management import SessionManager
from capslock.storage.async_database import (
    IncompatibleDatabaseError,
    MemoryDatabase,
    WorkspaceDatabase,
)
from capslock.storage.repositories import WorkspaceRepositories
from capslock.storage.schema import (
    MEMORY_APPLICATION_ID,
    MEMORY_SCHEMA_VERSION,
    WORKSPACE_APPLICATION_ID,
    WORKSPACE_SCHEMA_VERSION,
)
from tests.helpers import workspace_run


def test_empty_databases_initialize_with_distinct_application_ids(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = await WorkspaceDatabase.open(tmp_path / "workspace.sqlite3")
        memory = await MemoryDatabase.open(tmp_path / "memory.sqlite3")
        try:
            assert (await workspace.fetch_one("PRAGMA application_id"))[
                0
            ] == WORKSPACE_APPLICATION_ID
            assert (await workspace.fetch_one("PRAGMA user_version"))[
                0
            ] == WORKSPACE_SCHEMA_VERSION
            assert (await memory.fetch_one("PRAGMA application_id"))[
                0
            ] == MEMORY_APPLICATION_ID
            assert (await memory.fetch_one("PRAGMA user_version"))[
                0
            ] == MEMORY_SCHEMA_VERSION
            assert (await workspace.fetch_one("PRAGMA foreign_keys"))[0] == 1
            assert (await memory.fetch_one("PRAGMA secure_delete"))[0] == 1
            assert (await workspace.fetch_one("PRAGMA journal_mode"))[0] == "wal"
        finally:
            await workspace.close()
            await memory.close()

    asyncio.run(scenario())


def test_session_model_can_be_updated_and_restored(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "session-model.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("deepseek-v4-flash")
            updated = await repositories.sessions.set_model(
                session.id, "deepseek-v4-pro"
            )
            assert updated.model == "deepseek-v4-pro"
            assert (await repositories.sessions.require(session.id)).model == (
                "deepseek-v4-pro"
            )
        finally:
            await repositories.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "statement,values",
    [
        (
            "INSERT INTO work_items(id,session_id,question,status,position,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("w", "missing", "q", "queued", 0, "now", "now"),
        ),
        (
            "INSERT INTO sessions(id,model,created_at,updated_at,title,title_source) VALUES(?,?,?,?,?,?)",
            ("s", "m", "now", "now", "title", "invalid"),
        ),
    ],
)
def test_workspace_schema_enforces_foreign_keys_and_checks(
    tmp_path: Path, statement: str, values: tuple[object, ...]
) -> None:
    async def scenario() -> None:
        database = await WorkspaceDatabase.open(tmp_path / "state.sqlite3")
        try:
            with pytest.raises(aiosqlite.IntegrityError):
                await database.execute(statement, values)
        finally:
            await database.close()

    asyncio.run(scenario())


def test_workspace_schema_enforces_json_and_required_run_work_item(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            with pytest.raises(aiosqlite.IntegrityError):
                await repositories.database.execute(
                    "INSERT INTO run_events(run_id,sequence,event_kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (prepared.run.id, 1, "thinking", "not-json", "now"),
                )
            columns = await repositories.database.fetch_all("PRAGMA table_info(runs)")
            work_item = next(row for row in columns if row[1] == "work_item_id")
            assert work_item[3] == 1
            action_columns = {
                row[1]
                for row in await repositories.database.fetch_all(
                    "PRAGMA table_info(actions)"
                )
            }
            assert {"request_json", "result_json"} <= action_columns
            tables = {
                row[0]
                for row in await repositories.database.fetch_all(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert not {"file_action_data", "command_action_data"} & tables
            assert session.id == prepared.run.session_id
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_incompatible_database_is_rejected_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sentinel(value TEXT)")
    connection.execute("INSERT INTO sentinel VALUES('keep me')")
    connection.commit()
    connection.close()
    before = hashlib.sha256(path.read_bytes()).digest()

    async def scenario() -> None:
        with pytest.raises(IncompatibleDatabaseError, match="schema is not supported"):
            await WorkspaceDatabase.open(path)

    asyncio.run(scenario())
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT value FROM sentinel").fetchone()[0] == "keep me"


def test_wrong_schema_version_and_cross_database_are_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace_path = tmp_path / "workspace.sqlite3"
        workspace = await WorkspaceDatabase.open(workspace_path)
        await workspace.close()
        with pytest.raises(IncompatibleDatabaseError):
            await MemoryDatabase.open(workspace_path)

        with sqlite3.connect(workspace_path) as connection:
            connection.execute(f"PRAGMA user_version={WORKSPACE_SCHEMA_VERSION + 1}")
        with pytest.raises(IncompatibleDatabaseError):
            await WorkspaceDatabase.open(workspace_path)

    asyncio.run(scenario())


def test_canonical_layout_ignores_unmanaged_files(tmp_path: Path) -> None:
    user = UserLayout(tmp_path / "home", tmp_path / "memory.sqlite3")
    layout = ProjectLayout.discover(tmp_path, user=user)
    assert layout.database == tmp_path / ".capslock" / "state" / "capslock.sqlite3"
    unmanaged = tmp_path / "capslock.toml"
    unmanaged.write_text("[model]\n", encoding="utf-8")
    assert ProjectLayout.discover(tmp_path, user=user) == layout
    assert unmanaged.read_text(encoding="utf-8") == "[model]\n"


def test_layout_rejects_managed_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / ".capslock").symlink_to(target, target_is_directory=True)
    user = UserLayout(tmp_path / "home", tmp_path / "memory.sqlite3")
    with pytest.raises(LayoutConflict, match="symlink"):
        ProjectLayout.discover(tmp_path, user=user)


def test_current_state_reopens_without_mutation(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace_path = tmp_path / "workspace.sqlite3"
        memory_path = tmp_path / "memory.sqlite3"
        workspace = await WorkspaceDatabase.open(workspace_path)
        memory = await MemoryDatabase.open(memory_path)
        try:
            workspace_version = (await workspace.fetch_one("PRAGMA user_version"))[0]
            memory_version = (await memory.fetch_one("PRAGMA user_version"))[0]
            assert workspace_version == WORKSPACE_SCHEMA_VERSION == 8
            assert memory_version == MEMORY_SCHEMA_VERSION == 3
        finally:
            await workspace.close()
            await memory.close()

        assert not list(tmp_path.glob("backups/*"))
        workspace = await WorkspaceDatabase.open(workspace_path)
        memory = await MemoryDatabase.open(memory_path)
        try:
            assert (
                await workspace.fetch_one("PRAGMA user_version")
            )[0] == WORKSPACE_SCHEMA_VERSION
            assert (await memory.fetch_one("PRAGMA user_version"))[0] == 3
        finally:
            await workspace.close()
            await memory.close()

    asyncio.run(scenario())


def test_session_export_includes_all_snapshot_tables(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories, "Export this")
            await repositories.sessions.append_message(
                session.id, prepared.run.id, "user", "Export this"
            )
            manager = SessionManager(repositories, workspace=tmp_path)
            target = await manager.export(session.id, "exports/session")
            document = json.loads((target / "session.json").read_text(encoding="utf-8"))
            assert document["format"] == "capslock-session-export"
            assert document["version"] == 3
            assert document["sessions"][0]["id"] == session.id
            assert document["messages"][0]["content"] == "Export this"
            assert document["runs"][0]["work_item_id"] == prepared.work_item.id
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_session_delete_cascades_domain_rows_and_cleans_fts(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories, "Delete this")
            await repositories.sessions.append_message(
                session.id, prepared.run.id, "user", "searchable deletion marker"
            )
            await repositories.workflow.finalize(
                prepared.run.id,
                status=WorkItemStatus.COMPLETED,
                event_kind=AgentEventKind.COMPLETED,
                payload={"status": "completed"},
                duration_ms=1,
            )
            await repositories.sessions.delete(session.id)
            for table in ("sessions", "messages", "work_items", "runs", "run_events"):
                assert (
                    await repositories.database.fetch_one(
                        f"SELECT count(*) FROM {table}"
                    )
                )[0] == 0
            assert (
                await repositories.database.fetch_one(
                    "SELECT count(*) FROM session_search WHERE session_id=?",
                    (session.id,),
                )
            )[0] == 0
        finally:
            await repositories.close()

    asyncio.run(scenario())
