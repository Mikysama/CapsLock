import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from capslock.domain import MemoryScope, MemoryStatus, MemoryType
from capslock.config import Settings
from capslock.memory import MemoryService, default_memory_database
from capslock.model import ModelMessage, ModelResponse, ModelToolCall
from capslock.policy import PolicyError
from capslock.runtime import WorkspaceAgent
from capslock.session import SessionStore
from capslock.storage import MemoryStore
from capslock.storage import memory as memory_storage


def services(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = MemoryStore(tmp_path / "user" / "memory.sqlite3")
    return (
        store,
        MemoryService(store, workspace=first, session_id="session-one"),
        MemoryService(store, workspace=first, session_id="session-two"),
        MemoryService(store, workspace=second, session_id="session-three"),
    )


def test_memory_scopes_search_and_expiry_are_isolated(tmp_path: Path) -> None:
    store, first, other_session, other_workspace = services(tmp_path)
    global_item, _ = first.add(
        content="global Python preference", memory_type=MemoryType.PREFERENCE, scope=MemoryScope.GLOBAL
    )
    workspace_item, _ = first.add(
        content="workspace release decision", memory_type=MemoryType.DECISION, scope=MemoryScope.WORKSPACE
    )
    session_item, _ = first.add(
        content="session-only todo", memory_type=MemoryType.TODO, scope=MemoryScope.SESSION
    )
    expired, _ = first.add(
        content="expired note",
        memory_type=MemoryType.NOTE,
        scope=MemoryScope.GLOBAL,
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )

    assert {item.id for item in first.list()} == {global_item.id, workspace_item.id, session_item.id}
    assert {item.id for item in other_session.list()} == {global_item.id, workspace_item.id}
    assert {item.id for item in other_workspace.list()} == {global_item.id}
    assert first.search("release")[0].id == workspace_item.id
    assert first.search("todo")[0].id == session_item.id
    assert expired.id not in {item.id for item in first.search("expired")}
    assert expired.id in {item.id for item in first.list(include_inactive=True)}
    store.close()


def test_memory_redaction_edit_forget_undo_and_purge(tmp_path: Path) -> None:
    events = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "memory.sqlite3"
    with MemoryStore(path) as store:
        service = MemoryService(
            store,
            workspace=workspace,
            session_id="session",
            event=lambda kind, **data: events.append((kind, data)),
        )
        item, rules = service.add(
            content="api_key=raw-secret keep this",
            memory_type=MemoryType.FACT,
            scope=MemoryScope.WORKSPACE,
        )
        assert item.content == "api_key=<redacted> keep this"
        assert rules == ("secret_field",)
        edited, _ = service.edit(
            item.id,
            content="updated fact",
            memory_type=MemoryType.FACT,
            confidence=0.8,
            expires_at=None,
        )
        assert edited.revision == 2 and edited.content == "updated fact"
        assert service.forget(item.id).status is MemoryStatus.FORGOTTEN
        restored = service.undo(item.id)
        assert restored.status is MemoryStatus.ACTIVE and restored.content == "updated fact"
        purged = service.purge(item.id)
        assert purged.status is MemoryStatus.PURGED and purged.content is None
        assert store.connection.execute(
            "SELECT count(*) FROM memory_history WHERE memory_id=?", (item.id,)
        ).fetchone()[0] == 0
        audit = json.dumps([dict(row) for row in store.connection.execute("SELECT * FROM memory_audit")])
        assert "raw-secret" not in audit
    assert "raw-secret" not in path.read_bytes().decode("utf-8", errors="ignore")
    assert all("content" not in data for _, data in events)


def test_policy_import_export_and_atomic_validation(tmp_path: Path) -> None:
    store, first, _, _ = services(tmp_path)
    first.add(content="portable fact", memory_type=MemoryType.FACT, scope=MemoryScope.WORKSPACE)
    output, count = first.export_json(MemoryScope.WORKSPACE, "export.json")
    assert count == 1
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["format"] == "capslock-memory-export" and document["version"] == 2

    imported, _ = first.import_json(MemoryScope.SESSION, "export.json")
    assert len(imported) == 1 and imported[0].scope is MemoryScope.SESSION
    before = len(first.list(include_inactive=True))
    document["records"].append({"type": "invalid", "content": "bad"})
    (first.workspace / "invalid.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        first.import_json(MemoryScope.GLOBAL, "invalid.json")
    assert len(first.list(include_inactive=True)) == before

    first.set_local_write_enabled(False)
    with pytest.raises(PermissionError):
        first.add(content="blocked", memory_type=MemoryType.NOTE, scope=MemoryScope.GLOBAL)
    assert first.search("portable")
    assert first.export_json(MemoryScope.WORKSPACE, "disabled-export.json")[1] == 1
    outside = tmp_path / "outside"
    outside.mkdir()
    (first.workspace / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PolicyError, match="symbolic"):
        first.export_json(MemoryScope.WORKSPACE, "linked/export.json")
    with pytest.raises(PolicyError, match="relative"):
        first.export_json(MemoryScope.WORKSPACE, str(tmp_path / "absolute.json"))
    store.close()


def test_project_policy_and_memory_database_override(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "user" / "memory.sqlite3"
    (workspace / "capslock.toml").write_text("[memory]\nenabled=false\n", encoding="utf-8")
    monkeypatch.setenv("CAPSLOCK_MEMORY_DATABASE", str(database))
    settings = Settings.load(workspace)
    assert settings.memory.project_write_enabled is False
    assert settings.memory.database == database

    with MemoryStore(database) as store:
        readable = MemoryService(store, workspace=workspace, session_id="session")
        readable.add(content="existing global", memory_type=MemoryType.FACT, scope=MemoryScope.GLOBAL)
        blocked = MemoryService(
            store,
            workspace=workspace,
            session_id="session",
            project_write_enabled=settings.memory.project_write_enabled,
        )
        assert blocked.search("existing")
        with pytest.raises(PermissionError, match="capslock.toml"):
            blocked.add(content="blocked", memory_type=MemoryType.NOTE, scope=MemoryScope.GLOBAL)
    assert database.stat().st_mode & 0o777 == 0o600

    monkeypatch.setenv("CAPSLOCK_MEMORY_DATABASE", "relative.sqlite3")
    with pytest.raises(ValueError, match="absolute"):
        default_memory_database()


def test_memory_schema_rejects_newer_and_rolls_back_failed_upgrade(tmp_path: Path, monkeypatch) -> None:
    newer = tmp_path / "newer.sqlite3"
    connection = sqlite3.connect(newer)
    connection.execute("PRAGMA user_version = 3")
    connection.close()
    with pytest.raises(RuntimeError, match="newer"):
        MemoryStore(newer)

    legacy = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(legacy)
    connection.execute("CREATE TABLE legacy_marker(value TEXT)")
    connection.execute("INSERT INTO legacy_marker VALUES('kept')")
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        memory_storage,
        "MEMORY_SCHEMA",
        "CREATE TABLE partial(value TEXT); INVALID SQL",
    )
    with pytest.raises(RuntimeError, match="backup retained"):
        MemoryStore(legacy)
    connection = sqlite3.connect(legacy)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"legacy_marker"}
    assert connection.execute("SELECT value FROM legacy_marker").fetchone()[0] == "kept"
    connection.close()
    assert len(list((tmp_path / "backups").glob("legacy-schema-v0-*.sqlite3"))) == 1


class RecordingModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def complete(self, **kwargs):
        self.messages.append(kwargs["messages"])
        return self.responses.pop(0)


def test_model_memory_audit_citation_and_stale_context_isolation(tmp_path: Path) -> None:
    memory_store = MemoryStore(tmp_path / "user-memory.sqlite3")
    session_store = SessionStore(tmp_path / "workspace.sqlite3")
    seed = MemoryService(memory_store, workspace=tmp_path, session_id="seed")
    item, _ = seed.add(
        content="Remember Python 3.12",
        memory_type=MemoryType.FACT,
        scope=MemoryScope.GLOBAL,
    )
    model = RecordingModel([
        ModelResponse(ModelMessage(None, (ModelToolCall("search", "search_memories", '{"query":"Python"}'),))),
        ModelResponse(ModelMessage(f"Use Python 3.12. [[memory:{item.id}]]")),
        ModelResponse(ModelMessage('{"candidates":[]}')),
        ModelResponse(ModelMessage("Fresh answer.")),
        ModelResponse(ModelMessage('{"candidates":[]}')),
    ])
    agent = WorkspaceAgent(
        model,
        workspace=tmp_path,
        model="test",
        store=session_store,
        memory_store=memory_store,
    )
    answer = agent.ask("Which Python?")
    assert answer.citations == [item]
    audit_summary = session_store._connection.execute(
        "SELECT result_summary FROM tool_calls WHERE name='search_memories'"
    ).fetchone()[0]
    audit_arguments = session_store._connection.execute(
        "SELECT arguments FROM tool_calls WHERE name='search_memories'"
    ).fetchone()[0]
    assert item.id in audit_summary and "Python 3.12" not in audit_summary
    assert audit_arguments == "{}"

    agent.memory.forget(item.id)
    agent.ask("Continue")
    next_context = json.dumps(model.messages[-1])
    assert "Use Python 3.12" not in next_context
    assert "Which Python?" not in next_context
    assert answer.run_id in memory_store.excluded_runs(
        workspace=agent.memory.workspace_key, session_id=agent.session_id
    )
    session_store.close()
    memory_store.close()
