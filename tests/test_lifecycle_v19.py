from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import zipfile
from pathlib import Path

from rich.console import Console

from capslock.cli.app import async_main
from capslock.configuration import CONFIG_VERSION, Settings, read_config_document
from capslock.credentials import (
    credential_status,
    delete_keyring_credential,
    resolve_credential,
    set_keyring_credential,
)
from capslock.domain import (
    ActionStatus,
    ActionType,
    AgentEventKind,
    WorkItemStatus,
)
from capslock.layout import ProjectLayout
from capslock.lifecycle import LifecycleError, LifecycleService
from capslock.storage.memory_repositories import MemoryRepositories
from capslock.storage.repositories import WorkspaceRepositories
from capslock.storage.schema import MEMORY_SCHEMA_VERSION, WORKSPACE_SCHEMA_VERSION
from tests.helpers import workflow_service


def test_config_v0_migrates_atomically_and_init_is_noninteractive(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CAPSLOCK_API_KEY", "test-secret")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = workspace / ".capslock" / "config.toml"
    config.parent.mkdir()
    config.write_text("# keep me\n[model]\nmodel='old'\n", encoding="utf-8")
    settings = Settings.load(workspace)
    assert settings.model_config.model == "old"
    assert read_config_document(config)["config_version"] == CONFIG_VERSION
    assert "# keep me" in config.read_text(encoding="utf-8")
    assert list((config.parent / "state" / "backups").glob("config.v0.*.toml"))

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    output = io.StringIO()
    result = asyncio.run(
        async_main(
            [
                "--workspace",
                str(fresh),
                "init",
                "--non-interactive",
                "--base-url",
                "https://models.example.test/v1",
                "--model",
                "example-model",
                "--credential",
                "env:CAPSLOCK_API_KEY",
            ],
            console=Console(file=output, force_terminal=False),
        )
    )
    assert result == 0
    generated = read_config_document(fresh / ".capslock" / "config.toml")
    assert generated["config_version"] == CONFIG_VERSION
    assert generated["providers"]["primary"]["credential"] == "env:CAPSLOCK_API_KEY"
    assert "test-secret" not in (fresh / ".capslock" / "config.toml").read_text()

    doctor_output = io.StringIO()
    result = asyncio.run(
        async_main(
            ["--workspace", str(fresh), "doctor", "--json"],
            console=Console(file=doctor_output, force_terminal=False),
        )
    )
    assert result == 0
    diagnostics = json.loads(doctor_output.getvalue())["diagnostics"]
    assert any(
        item["code"] == "config" and item["message"] == "version 2 valid"
        for item in diagnostics
    )


def test_environment_and_keyring_credentials_are_secret_safe(monkeypatch) -> None:
    values: dict[tuple[str, str], str] = {}

    class FakeKeyring:
        def get_password(self, service: str, name: str):
            return values.get((service, name))

        def set_password(self, service: str, name: str, secret: str):
            values[(service, name)] = secret

        def delete_password(self, service: str, name: str):
            values.pop((service, name), None)

    monkeypatch.setattr("capslock.credentials._keyring", lambda: FakeKeyring())
    monkeypatch.setenv("MODEL_KEY", "env-secret")
    assert resolve_credential("env:MODEL_KEY") == "env-secret"
    set_keyring_credential("primary", "keyring-secret")
    assert resolve_credential("keyring:primary") == "keyring-secret"
    assert credential_status("keyring:primary").available
    delete_keyring_credential("primary")
    assert not credential_status("keyring:primary").available


def test_schema_v3_backup_verification_and_tamper_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        layout = ProjectLayout.discover(workspace)
        repositories = await WorkspaceRepositories.open(
            layout.database, workspace=workspace
        )
        memory = await MemoryRepositories.open(layout.user.memory)
        await repositories.close()
        await memory.close()
        layout.config.write_text(
            'config_version = 1\n[model]\napi_key = "backup-secret"\n',
            encoding="utf-8",
        )
        layout.local_mcp.parent.mkdir(parents=True, exist_ok=True)
        layout.local_mcp.write_text(
            '{"servers":{"demo":{"env":{"TOKEN":"mcp-secret"}}}}',
            encoding="utf-8",
        )
        assert _version(layout.database) == WORKSPACE_SCHEMA_VERSION == 5
        assert _version(layout.user.memory) == MEMORY_SCHEMA_VERSION == 3
        service = LifecycleService(layout)
        backup = service.backup_create(tmp_path / "state.clbackup")
        assert service.verify(backup)["format"] == "capslock-backup"
        with zipfile.ZipFile(backup) as archive:
            assert b"backup-secret" not in archive.read("config.toml")
            assert b"mcp-secret" not in archive.read("local-mcp.json")
        with zipfile.ZipFile(backup, "a") as archive:
            archive.writestr("events.jsonl", "tampered")
        try:
            service.verify(backup)
        except LifecycleError as exc:
            assert "checksum" in str(exc) or "file list" in str(exc)
        else:
            raise AssertionError("tampered backup was accepted")

    asyncio.run(scenario())


def test_v18_schema_v2_is_backed_up_and_migrated_to_v3(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        layout = ProjectLayout.discover(workspace)
        repositories = await WorkspaceRepositories.open(
            layout.database, workspace=workspace
        )
        memory = await MemoryRepositories.open(layout.user.memory)
        await repositories.close()
        await memory.close()
        connection = sqlite3.connect(layout.database)
        connection.execute("PRAGMA foreign_keys=OFF")
        for column in ("requires_reapproval", "historical_only", "import_id"):
            connection.execute(f"ALTER TABLE actions DROP COLUMN {column}")
        connection.execute("DROP TABLE lifecycle_import_items")
        connection.execute("DROP TABLE lifecycle_imports")
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.close()
        connection = sqlite3.connect(layout.user.memory)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE lifecycle_import_items")
        connection.execute("DROP TABLE lifecycle_imports")
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.close()
        repositories = await WorkspaceRepositories.open(
            layout.database, workspace=workspace
        )
        memory = await MemoryRepositories.open(layout.user.memory)
        await repositories.close()
        await memory.close()
        assert _version(layout.database) == WORKSPACE_SCHEMA_VERSION
        assert _version(layout.user.memory) == 3
        assert list(layout.database.parent.joinpath("backups").glob("*.schema-2.*.bak"))
        assert list(
            layout.user.memory.parent.joinpath("backups").glob("*.schema-2.*.bak")
        )

    asyncio.run(scenario())


def test_portable_import_is_idempotent_and_resets_approval(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        target = tmp_path / "target"
        source.mkdir()
        target.mkdir()
        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "source-home"))
        source_layout = ProjectLayout.discover(source)
        repositories = await WorkspaceRepositories.open(
            source_layout.database, workspace=source
        )
        memory = await MemoryRepositories.open(source_layout.user.memory)
        session = await repositories.sessions.create("model")
        prepared = await workflow_service(repositories).prepare(session.id, "question")
        action = await repositories.actions.create(
            session_id=session.id,
            run_id=prepared.run.id,
            action_type=ActionType.FILE_CREATE,
            summary="create file",
            request={
                "path": "result.txt",
                "operation": "create",
                "after_content": "content",
                "expected_hash": None,
            },
        )
        await repositories.actions.transition(action.id, ActionStatus.APPROVED)
        await workflow_service(repositories).finish(
            prepared.run.id,
            status=WorkItemStatus.WAITING_APPROVAL,
            event_kind=AgentEventKind.WAITING_APPROVAL,
            payload={"action_ids": [action.id]},
            duration_ms=1,
        )
        await repositories.close()
        await memory.close()
        portable = LifecycleService(source_layout).export(tmp_path / "data.clexport")

        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "target-home"))
        target_layout = ProjectLayout.discover(target)
        repositories = await WorkspaceRepositories.open(
            target_layout.database, workspace=target
        )
        memory = await MemoryRepositories.open(target_layout.user.memory)
        await repositories.database.execute(
            """INSERT INTO sessions(
               id,model,created_at,updated_at,summary,title,title_source,title_updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                session.id,
                "different-model",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "",
                "collision",
                "manual",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        await repositories.close()
        await memory.close()
        service = LifecycleService(target_layout)
        first = service.import_archive(portable)
        second = service.import_archive(portable)
        assert first == second
        repositories = await WorkspaceRepositories.open(
            target_layout.database, workspace=target
        )
        sessions = await repositories.sessions.list(10)
        imported_session = first["mappings"]["sessions"][session.id]
        actions = await repositories.actions.list(imported_session)
        assert len(sessions) == 2
        assert imported_session != session.id
        assert first["remapped"] >= 1
        assert actions[0].status is ActionStatus.PENDING
        assert actions[0].requires_reapproval
        assert not actions[0].historical_only
        imports = await repositories.database.fetch_one(
            "SELECT count(*) FROM lifecycle_imports WHERE status='completed'"
        )
        assert int(imports[0]) == 1
        await repositories.close()

    asyncio.run(scenario())


def test_doctor_reports_invalid_config_without_rewriting(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    config = workspace / ".capslock" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("not = [valid", encoding="utf-8")
    before = config.read_bytes()
    output = io.StringIO()
    result = asyncio.run(
        async_main(
            ["--workspace", str(workspace), "doctor", "--json"],
            console=Console(file=output, force_terminal=False),
        )
    )
    assert result == 1
    assert config.read_bytes() == before
    payload = json.loads(output.getvalue())
    assert any(item["code"] == "config_parse" for item in payload["diagnostics"])


def _version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
