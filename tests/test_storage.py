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
from capslock.planning import PlanningService
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


def test_resumed_transcript_uses_run_order_and_keeps_turn_roles_together(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "transcript-order.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            for question, answer_text in (
                ("first question", "first answer"),
                ("second question", "second answer"),
            ):
                item = await repositories.work_items.enqueue(session.id, question)
                run = await repositories.workflow.start_run(
                    session.id, item.id, item.question
                )
                await repositories.sessions.append_message(
                    session.id, run.id, "user", question
                )
                await repositories.sessions.append_message(
                    session.id, run.id, "assistant", answer_text
                )
                await repositories.workflow.finalize(
                    run.id,
                    status=WorkItemStatus.COMPLETED,
                    event_kind=AgentEventKind.COMPLETED,
                    payload={"status": "completed"},
                    duration_ms=1,
                )

            # Imported and replayed sessions may contain timestamps that collide or
            # move backwards. They must not override durable run/message ordering.
            await repositories.database.execute(
                """UPDATE messages SET created_at=CASE
                   WHEN content LIKE 'first %' THEN '2026-01-02T00:00:00+00:00'
                   ELSE '2026-01-01T00:00:00+00:00' END"""
            )
            await repositories.database.execute(
                """UPDATE runs SET started_at=CASE
                   WHEN question='first question' THEN '2026-01-02T00:00:00+00:00'
                   ELSE '2026-01-01T00:00:00+00:00' END"""
            )

            transcript = await repositories.sessions.transcript(session.id)
            assert [(entry["role"], entry["content"]) for entry in transcript] == [
                ("user", "first question"),
                ("assistant", "first answer"),
                ("user", "second question"),
                ("assistant", "second answer"),
            ]
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
            assert workspace_version == WORKSPACE_SCHEMA_VERSION == 20
            assert memory_version == MEMORY_SCHEMA_VERSION == 6
        finally:
            await workspace.close()
            await memory.close()

        assert not list(tmp_path.glob("backups/*"))
        workspace = await WorkspaceDatabase.open(workspace_path)
        memory = await MemoryDatabase.open(memory_path)
        try:
            assert (await workspace.fetch_one("PRAGMA user_version"))[
                0
            ] == WORKSPACE_SCHEMA_VERSION
            assert (await memory.fetch_one("PRAGMA user_version"))[0] == 6
        finally:
            await workspace.close()
            await memory.close()

    asyncio.run(scenario())


def test_workspace_schema_sixteen_adds_compaction_policy_and_quality(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace-v16.sqlite3"

    async def initialize() -> None:
        database = await WorkspaceDatabase.open(path)
        await database.close()

    asyncio.run(initialize())
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            ALTER TABLE context_compactions DROP COLUMN summary_policy_digest;
            ALTER TABLE context_compactions DROP COLUMN result_tokens;
            ALTER TABLE context_compactions DROP COLUMN quality_status;
            ALTER TABLE context_summary_segments RENAME TO segments_current;
            CREATE TABLE context_summary_segments (
              id TEXT PRIMARY KEY,
              source_digest TEXT NOT NULL,
              model_profile TEXT NOT NULL,
              summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
              source_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(source_refs_json)),
              input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
              output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
              created_at TEXT NOT NULL,
              UNIQUE(source_digest,model_profile)
            ) STRICT;
            INSERT INTO context_summary_segments VALUES(
              'segment-old','digest','fast','{}','[]',1,2,'created'
            );
            DROP TABLE segments_current;
            PRAGMA user_version=16;
            """
        )

    async def upgrade() -> None:
        database = await WorkspaceDatabase.open(path)
        try:
            assert (await database.fetch_one("PRAGMA user_version"))[0] == 20
            columns = {
                row[1]
                for row in await database.fetch_all(
                    "PRAGMA table_info(context_compactions)"
                )
            }
            assert {
                "summary_policy_digest",
                "result_tokens",
                "quality_status",
            } <= columns
            segment = await database.fetch_one(
                """SELECT summary_policy_digest,input_tokens,output_tokens
                   FROM context_summary_segments WHERE id='segment-old'"""
            )
            assert tuple(segment) == ("", 1, 2)
        finally:
            await database.close()

    asyncio.run(upgrade())


def test_workspace_and_memory_migrations_are_backup_first(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "workspace.sqlite3"
    memory_path = tmp_path / "memory.sqlite3"

    async def initialize() -> None:
        workspace = await WorkspaceDatabase.open(workspace_path)
        memory = await MemoryDatabase.open(memory_path)
        await workspace.close()
        await memory.close()

    asyncio.run(initialize())
    with sqlite3.connect(workspace_path) as connection:
        connection.executescript(
            """
            DROP TRIGGER episodic_tool_artifacts_ai;
            DROP TRIGGER episodic_tool_invocations_ai;
            DROP TRIGGER episodic_messages_ai;
            DROP TRIGGER episodic_documents_au;
            DROP TRIGGER episodic_documents_ad;
            DROP TRIGGER episodic_documents_ai;
            DROP TABLE episodic_fts;
            DROP TABLE episodic_documents;
            DROP TABLE context_summary_segments;
            ALTER TABLE tool_artifacts DROP COLUMN index_content;
            PRAGMA user_version=15;
            """
        )
    with sqlite3.connect(memory_path) as connection:
        connection.executescript(
            """
            DROP TABLE memory_extraction_segments;
            ALTER TABLE memories DROP COLUMN owner_session_id;
            ALTER TABLE memories DROP COLUMN project_instance_id;
            ALTER TABLE memory_workspace_settings DROP COLUMN temporary_ttl_days;
            ALTER TABLE memory_candidates DROP COLUMN extractor_confidence;
            ALTER TABLE memory_candidates DROP COLUMN verifier_confidence;
            ALTER TABLE memory_candidates DROP COLUMN verification_status;
            ALTER TABLE memory_candidates DROP COLUMN instruction_like;
            ALTER TABLE memory_candidates DROP COLUMN calibration_version;
            PRAGMA user_version=4;
            """
        )

    async def upgrade() -> None:
        workspace = await WorkspaceDatabase.open(workspace_path)
        memory = await MemoryDatabase.open(memory_path)
        try:
            assert (await workspace.fetch_one("PRAGMA user_version"))[0] == 20
            assert (await memory.fetch_one("PRAGMA user_version"))[0] == 6
            assert await workspace.fetch_one(
                "SELECT 1 FROM sqlite_master WHERE name='episodic_documents'"
            )
            assert await memory.fetch_one(
                "SELECT 1 FROM sqlite_master WHERE name='memory_extraction_segments'"
            )
        finally:
            await workspace.close()
            await memory.close()

    asyncio.run(upgrade())
    backups = {item.name for item in (tmp_path / "backups").iterdir()}
    assert any(name.startswith("capslock-v15-") for name in backups)
    assert any(name.startswith("memory-v4-") for name in backups)


def test_workspace_schema_ten_upgrades_permission_state_to_twelve(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace-upgrade.sqlite3"

    async def initialize() -> None:
        database = await WorkspaceDatabase.open(path)
        await database.close()

    asyncio.run(initialize())
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
PRAGMA foreign_keys=OFF;
DROP TABLE permission_grants;
DROP TABLE permission_requests;
ALTER TABLE permission_decisions RENAME TO permission_decisions_current;
CREATE TABLE permission_decisions (
  id TEXT PRIMARY KEY,
  invocation_id TEXT NOT NULL REFERENCES tool_invocations(id) ON DELETE CASCADE,
  behavior TEXT NOT NULL CHECK(behavior IN ('allow','ask','deny')),
  source TEXT NOT NULL,
  reason TEXT NOT NULL,
  rule_json TEXT CHECK(rule_json IS NULL OR json_valid(rule_json)),
  classifier_json TEXT CHECK(classifier_json IS NULL OR json_valid(classifier_json)),
  decided_by TEXT,
  created_at TEXT NOT NULL
) STRICT;
DROP TABLE permission_decisions_current;
ALTER TABLE permission_rules RENAME TO permission_rules_current;
CREATE TABLE permission_rules (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
  behavior TEXT NOT NULL CHECK(behavior IN ('allow','ask','deny')),
  tool TEXT NOT NULL,
  constraints_json TEXT NOT NULL CHECK(json_valid(constraints_json)),
  source TEXT NOT NULL CHECK(source='session'),
  created_at TEXT NOT NULL
) STRICT;
DROP TABLE permission_rules_current;
PRAGMA user_version=10;
"""
        )

    async def upgrade() -> None:
        database = await WorkspaceDatabase.open(path)
        try:
            assert (await database.fetch_one("PRAGMA user_version"))[0] == 20
            tables = {
                row[0]
                for row in await database.fetch_all(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert {
                "permission_requests",
                "permission_grants",
                "session_plans",
                "plan_revisions",
                "plan_requests",
                "plan_implementations",
            } <= tables
            decision_columns = {
                row[1]
                for row in await database.fetch_all(
                    "PRAGMA table_info(permission_decisions)"
                )
            }
            assert {
                "reason_code",
                "mode",
                "arguments_sha256",
                "suggestions_json",
            } <= decision_columns
            request_columns = {
                row[1]
                for row in await database.fetch_all(
                    "PRAGMA table_info(permission_requests)"
                )
            }
            assert "result_json" in request_columns
        finally:
            await database.close()

    asyncio.run(upgrade())
    assert len(list((tmp_path / "backups").glob("capslock-v10-*.sqlite3"))) == 1


def test_workspace_schema_eleven_upgrades_plan_state_to_twelve(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace-v11.sqlite3"

    async def initialize() -> None:
        database = await WorkspaceDatabase.open(path)
        await database.close()

    asyncio.run(initialize())
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
PRAGMA foreign_keys=OFF;
DROP TABLE plan_implementations;
DROP TABLE plan_requests;
DROP TABLE plan_revisions;
DROP TABLE session_plans;
PRAGMA user_version=11;
"""
        )

    async def upgrade() -> None:
        database = await WorkspaceDatabase.open(path)
        try:
            assert (await database.fetch_one("PRAGMA user_version"))[0] == 20
            tables = {
                row[0]
                for row in await database.fetch_all(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert {
                "session_plans",
                "plan_revisions",
                "plan_requests",
                "plan_implementations",
            } <= tables
        finally:
            await database.close()

    asyncio.run(upgrade())
    assert len(list((tmp_path / "backups").glob("capslock-v11-*.sqlite3"))) == 1


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
            assert document["version"] == 7
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
            plan, _ = await PlanningService(
                repositories.plans,
                root=tmp_path / ".capslock" / "state" / "plans",
            ).create(
                session.id,
                "Delete this plan",
                entry_source="slash",
                base_permission_mode="approve_for_me",
            )
            mirror = (
                tmp_path / ".capslock" / "state" / "plans" / plan.mirror_relative_path
            )
            assert mirror.is_file()
            await repositories.sessions.delete(session.id)
            assert not mirror.exists()
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
