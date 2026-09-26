"""Backup-first upgrades and portable telemetry compatibility."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import stat
import zipfile

import aiosqlite
import pytest

from capslock.configuration.loader import load_config_document
from capslock.layout import ProjectLayout, UserLayout
from capslock.lifecycle import LifecycleService
from capslock.session_management import SessionManager
from capslock.storage.memory_repositories import MemoryRepositories
from capslock.storage.repositories import WorkspaceRepositories
from capslock.storage.schema import MEMORY_SCHEMA_VERSION
from capslock.storage.upgrades import upgrade_workspace_schema
from tests.helpers import workflow_service


DETAIL_FIELDS = (
    "cached_input_tokens",
    "reasoning_tokens",
    "usage_source",
    "request_id",
    "first_token_ms",
    "retry_delay_ms",
    "output_started",
    "price_snapshot_json",
)


def config_text():
    return """# preserve this comment
config_version = 13
[providers.main]
kind = "openai_responses"
base_url = "https://example.invalid"
credential = "env:CAPSLOCK_TEST_KEY"
[models.main]
provider = "main"
model = "test"
[routing]
reasoning = ["main"]
[context]
trigger_ratio = 0.85
target_ratio = 0.65
[tools]
selection_mode = "full"
"""


def test_current_config_upgrade_changes_only_version_and_is_private(tmp_path):
    import tomllib

    path = tmp_path / "config.toml"
    source = config_text()
    path.write_text(source)
    document = load_config_document(path)
    expected = tomllib.loads(source)
    expected["config_version"] = 14
    assert document == expected
    (backup,) = tmp_path.glob("*.bak")
    assert backup.read_text() == source
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "# preserve this comment" in path.read_text()
    assert load_config_document(path) == document
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_invalid_config_upgrade_preserves_source(tmp_path):
    path = tmp_path / "config.toml"
    source = config_text().replace("target_ratio = 0.65", "target_ratio = 1.5")
    path.write_text(source)
    with pytest.raises(ValueError):
        load_config_document(path)
    assert path.read_text() == source


def test_config_replace_failure_keeps_source_and_removes_temporary(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.toml"
    source = config_text()
    path.write_text(source)

    def fail(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("capslock.configuration.loader.os.replace", fail)
    with pytest.raises(OSError, match="simulated"):
        load_config_document(path)
    assert path.read_text() == source
    assert not list(tmp_path.glob(".config-*"))
    (backup,) = tmp_path.glob("*.bak")
    assert backup.read_text() == source


async def seed_layout(workspace, home):
    workspace.mkdir(exist_ok=True)
    layout = ProjectLayout.discover(workspace, user=UserLayout(home))
    repositories = await WorkspaceRepositories.open(
        layout.database, workspace=workspace
    )
    memory = await MemoryRepositories.open(layout.user.memory)
    try:
        session = await repositories.sessions.create("example-model")
        run = await workflow_service(repositories).prepare(
            session.id, "migration question"
        )
        decision = await repositories.models.record_decision(
            run.run.id, role="reasoning", candidates=[], selected="main", reasons={}
        )
        call = await repositories.models.start_call(
            run.run.id,
            decision_id=decision,
            role="reasoning",
            profile="main",
            provider="main",
            model="example-model",
            attempt=1,
            data_policy="local",
            fallback_from=None,
        )
        await repositories.models.finish_call(
            call, duration_ms=5, input_tokens=12, output_tokens=7
        )
        exported = await SessionManager(repositories, workspace=workspace).export(
            session.id, "session.json"
        )
        assert json.loads((exported / "session.json").read_text())["version"] == 8
    finally:
        await repositories.close()
        await memory.close()
    return layout


def downgrade_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE sessions DROP COLUMN model_profile")
        for field in DETAIL_FIELDS:
            connection.execute(f"ALTER TABLE model_calls DROP COLUMN {field}")
        connection.execute("PRAGMA user_version=20")


def test_database_upgrade_rollback_and_retry_preserves_rows(tmp_path, monkeypatch):
    async def scenario():
        layout = await seed_layout(tmp_path / "source", tmp_path / "home")
        downgrade_database(layout.database)
        original = __import__(
            "capslock.storage.upgrades", fromlist=["_validate_integrity"]
        )._validate_integrity

        async def fail(connection, label):
            await original(connection, label)
            raise ValueError("injected integrity failure")

        monkeypatch.setattr("capslock.storage.upgrades._validate_integrity", fail)
        connection = await aiosqlite.connect(layout.database)
        try:
            with pytest.raises(ValueError, match="injected"):
                await upgrade_workspace_schema(layout.database, connection)
            assert (await (await connection.execute("PRAGMA user_version")).fetchone())[
                0
            ] == 20
            assert "model_profile" not in {
                row[1]
                for row in await (
                    await connection.execute("PRAGMA table_info(sessions)")
                ).fetchall()
            }
            assert (
                await (
                    await connection.execute("SELECT input_tokens FROM model_calls")
                ).fetchone()
            )[0] == 12
            monkeypatch.setattr(
                "capslock.storage.upgrades._validate_integrity", original
            )
            await upgrade_workspace_schema(layout.database, connection)
            assert (await (await connection.execute("PRAGMA user_version")).fetchone())[
                0
            ] == 21
            row = await (
                await connection.execute(
                    "SELECT " + ",".join(DETAIL_FIELDS) + " FROM model_calls"
                )
            ).fetchone()
            assert tuple(row) == (None,) * len(DETAIL_FIELDS)
        finally:
            await connection.close()
        backup_count = len(
            list(layout.database.parent.joinpath("backups").glob("*.sqlite3"))
        )
        repositories = await WorkspaceRepositories.open(
            layout.database, workspace=layout.workspace
        )
        await repositories.close()
        assert (
            len(list(layout.database.parent.joinpath("backups").glob("*.sqlite3")))
            == backup_count
        )
        with sqlite3.connect(layout.user.memory) as memory:
            assert (
                memory.execute("PRAGMA user_version").fetchone()[0]
                == MEMORY_SCHEMA_VERSION
                == 6
            )

    asyncio.run(scenario())


def test_database_restoration_survives_migration_report_write_failure(
    tmp_path, monkeypatch
):
    async def scenario():
        layout = await seed_layout(tmp_path / "source", tmp_path / "home")
        downgrade_database(layout.database)
        calls = 0

        async def fail_after_commit(connection, label):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("post-commit validation failure")

        def report_failure(*args):
            raise OSError("diagnostic storage unavailable")

        monkeypatch.setattr(
            "capslock.storage.upgrades._validate_integrity", fail_after_commit
        )
        monkeypatch.setattr(
            "capslock.storage.upgrades._write_migration_report", report_failure
        )
        connection = await aiosqlite.connect(layout.database)
        try:
            with pytest.raises(Exception):
                await upgrade_workspace_schema(layout.database, connection)
            assert (await (await connection.execute("PRAGMA user_version")).fetchone())[
                0
            ] == 20
            assert "model_profile" not in {
                row[1]
                for row in await (
                    await connection.execute("PRAGMA table_info(sessions)")
                ).fetchall()
            }
        finally:
            await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("archive_version", [7, 8])
def test_portable_old_and_current_archives_keep_unknown_telemetry_null(
    tmp_path, archive_version
):
    async def scenario():
        source = await seed_layout(tmp_path / "source", tmp_path / "source-home")
        archive = LifecycleService(source).export(tmp_path / "original.clexport")
        with zipfile.ZipFile(archive) as bundle:
            files = {name: bundle.read(name) for name in bundle.namelist()}
        manifest = json.loads(files["manifest.json"])
        assert manifest["version"] == 8
        if archive_version == 7:
            data = json.loads(files["data.json"])
            for row in data["workspace"]["sessions"]:
                row.pop("model_profile", None)
            for row in data["workspace"]["model_calls"]:
                for field in DETAIL_FIELDS:
                    row.pop(field, None)
            files["data.json"] = json.dumps(data).encode()
            manifest["version"] = 7
            manifest["files"]["data.json"] = hashlib.sha256(
                files["data.json"]
            ).hexdigest()
            files["manifest.json"] = json.dumps(manifest).encode()
        portable = tmp_path / "portable.clexport"
        with zipfile.ZipFile(portable, "w") as bundle:
            for name, content in files.items():
                bundle.writestr(name, content)
        target_root = tmp_path / "target"
        target_root.mkdir()
        target = ProjectLayout.discover(
            target_root, user=UserLayout(tmp_path / "target-home")
        )
        repositories = await WorkspaceRepositories.open(
            target.database, workspace=target_root
        )
        memory = await MemoryRepositories.open(target.user.memory)
        await repositories.close()
        await memory.close()
        service = LifecycleService(target)
        service.import_archive(portable)
        service.import_archive(portable)
        with sqlite3.connect(target.database) as database:
            assert (
                database.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 1
            )
            assert database.execute(
                "SELECT " + ",".join(DETAIL_FIELDS) + " FROM model_calls"
            ).fetchone() == (None,) * len(DETAIL_FIELDS)
            assert database.execute(
                "SELECT input_tokens,output_tokens FROM model_calls"
            ).fetchone() == (12, 7)
        roundtrip = service.export(tmp_path / "roundtrip.clexport")
        with zipfile.ZipFile(roundtrip) as bundle:
            assert json.loads(bundle.read("manifest.json"))["version"] == 8
            row = json.loads(bundle.read("data.json"))["workspace"]["model_calls"][0]
            assert all(row[field] is None for field in DETAIL_FIELDS)

    asyncio.run(scenario())
