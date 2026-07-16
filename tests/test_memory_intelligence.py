from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from capslock.domain import (
    EmbeddingBackend,
    MemoryCandidateStatus,
    MemoryOrigin,
    MemoryPolicy,
    MemoryScope,
    MemoryType,
)
from capslock.embeddings import cosine_similarity, pack_vector, unpack_vector, validate_loopback_endpoint
from capslock.memory import MemoryService
from capslock.model import ModelMessage, ModelResponse, ModelUsage
from capslock.storage import MemoryStore


class QueueModel:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        return ModelResponse(ModelMessage(self.contents.pop(0)), ModelUsage(11, 7))


class KeywordProvider:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [
                1.0 if "rust" in text.casefold() else 0.0,
                1.0 if "python" in text.casefold() else 0.0,
                0.5,
            ]
            for text in texts
        ]


def service(tmp_path: Path, *, workspace_name: str = "workspace", **kwargs) -> tuple[MemoryStore, MemoryService]:
    workspace = tmp_path / workspace_name
    workspace.mkdir(exist_ok=True)
    store = MemoryStore(tmp_path / "memory.sqlite3")
    return store, MemoryService(store, workspace=workspace, session_id="session", **kwargs)


def candidate_json(*, content: str, scope: str = "workspace", confidence: float = 0.95) -> str:
    return (
        '{"candidates":[{"content":'
        + repr(content).replace("'", '"')
        + f',"type":"preference","scope":"{scope}","confidence":{confidence},"direct":true}}]}}'
    )


def test_memory_schema_v1_upgrades_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE memories (
          id TEXT PRIMARY KEY, content TEXT, memory_type TEXT NOT NULL, scope TEXT NOT NULL,
          workspace_key TEXT, session_id TEXT, source_kind TEXT NOT NULL, source_ref TEXT,
          confidence REAL NOT NULL, expires_at TEXT, revision INTEGER NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, purged_at TEXT
        );
        CREATE TABLE memory_workspace_settings (workspace_key TEXT PRIMARY KEY, write_enabled INTEGER NOT NULL);
        PRAGMA user_version=1;
        """
    )
    connection.close()
    with MemoryStore(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in store.connection.execute("PRAGMA table_info(memories)")}
        assert {"origin", "source_valid"} <= columns
        assert store.memory_settings("workspace")["policy"] is MemoryPolicy.REVIEW
    assert len(list((tmp_path / "backups").glob("memory-schema-v1-*.sqlite3"))) == 1


def test_capture_policy_off_and_review_queue(tmp_path: Path) -> None:
    store, memory = service(tmp_path)
    model = QueueModel(candidate_json(content="I prefer Rust"))
    memory.set_policy(MemoryPolicy.OFF)
    assert memory.capture_candidates(
        model, model="test", run_id="run-off", question="I prefer Rust", answer="OK"
    ).candidates == 0
    assert model.calls == 0

    memory.set_policy(MemoryPolicy.REVIEW)
    result = memory.capture_candidates(
        model, model="test", run_id="run-review", question="I prefer Rust", answer="OK"
    )
    assert result.candidates == 1 and result.input_tokens == 11 and result.output_tokens == 7
    candidate = memory.candidates()[0]
    assert candidate.status is MemoryCandidateStatus.PENDING
    assert memory.list() == []
    accepted = memory.accept_candidate(candidate.id)
    assert accepted.origin is MemoryOrigin.REVIEWED
    assert memory.resolve_candidate(candidate.id).content is None
    store.close()


def test_automatic_only_adopts_low_risk_non_global_candidates(tmp_path: Path) -> None:
    store, memory = service(tmp_path)
    memory.set_policy(MemoryPolicy.AUTOMATIC)
    model = QueueModel(
        candidate_json(content="I prefer Rust"),
        candidate_json(content="I prefer concise output", scope="global"),
        candidate_json(content="token ghp_abcdefghijklmnopqrstuvwxyz1234567890"),
    )
    first = memory.capture_candidates(
        model, model="test", run_id="run-one", question="I prefer Rust", answer="OK"
    )
    assert first.adopted == 1
    assert memory.list()[0].origin is MemoryOrigin.AUTOMATIC
    second = memory.capture_candidates(
        model, model="test", run_id="run-two", question="I prefer concise output", answer="OK"
    )
    third = memory.capture_candidates(
        model, model="test", run_id="run-three", question="remember my token", answer="OK"
    )
    assert second.adopted == 0 and third.adopted == 0
    pending = memory.candidates()
    assert any("global_scope" in item.risk_flags for item in pending)
    assert any(item.risk_flags for item in pending if item.source_run_id == "run-three")
    store.close()


def test_automatic_merges_duplicate_source_and_blocks_conflict(tmp_path: Path) -> None:
    store, memory = service(tmp_path)
    existing, _ = memory.add(
        content="Use SQLite for the database",
        memory_type=MemoryType.PREFERENCE,
        scope=MemoryScope.WORKSPACE,
    )
    memory.set_policy(MemoryPolicy.AUTOMATIC)
    duplicate = QueueModel(candidate_json(content="Use SQLite for the database"))
    result = memory.capture_candidates(
        duplicate,
        model="test",
        run_id="run-duplicate",
        question="Use SQLite for the database",
        answer="OK",
    )
    assert result.adopted == 1
    decided = memory.resolve_candidate(memory.candidates(include_all=True)[0].id)
    assert decided.status is MemoryCandidateStatus.DUPLICATE
    assert decided.adopted_memory_id == existing.id

    conflict = QueueModel(
        candidate_json(content="Use PostgreSQL for the database"),
        f'{{"relation":"conflict","memory_id":"{existing.id}"}}',
    )
    result = memory.capture_candidates(
        conflict,
        model="test",
        run_id="run-conflict",
        question="Use PostgreSQL for the database",
        answer="OK",
    )
    assert result.adopted == 0
    pending = [item for item in memory.candidates() if item.source_run_id == "run-conflict"]
    assert pending[0].status is MemoryCandidateStatus.CONFLICT
    assert pending[0].related_memory_id == existing.id
    store.close()


def test_recall_is_scoped_explainable_and_invalidates_automatic_source(tmp_path: Path) -> None:
    store, memory = service(tmp_path, source_validator=lambda run_id: run_id != "missing-run")
    memory.add(
        content="The workspace uses Python 3.12",
        memory_type=MemoryType.FACT,
        scope=MemoryScope.WORKSPACE,
    )
    other = MemoryService(store, workspace=tmp_path / "other", session_id="other")
    other.workspace.mkdir(exist_ok=True)
    other.add(
        content="Other workspace Python 2",
        memory_type=MemoryType.FACT,
        scope=MemoryScope.WORKSPACE,
    )
    automatic = store.create(
        content="Rust source is missing",
        memory_type=MemoryType.FACT,
        scope=MemoryScope.WORKSPACE,
        workspace=memory.workspace_key,
        session_id=None,
        source_kind="conversation",
        source_ref="missing-run",
        confidence=1,
        expires_at=None,
        origin=MemoryOrigin.AUTOMATIC,
        run_id="missing-run",
    )
    hits = memory.recall("Which Python does this workspace use?", run_id="recall-run")
    assert len(hits) == 1
    assert hits[0].memory.content == "The workspace uses Python 3.12"
    assert hits[0].reasons and memory.context("recall-run")[0].score == hits[0].score
    memory.recall("Rust", run_id="invalid-source-run")
    assert store.get(automatic.id, include_inactive=True).source_valid is False
    store.close()


def test_embedding_helpers_and_semantic_recall(tmp_path: Path) -> None:
    def factory(backend, model, endpoint):
        return KeywordProvider()

    store, memory = service(tmp_path, embedding_provider_factory=factory)
    memory.configure_embeddings(EmbeddingBackend.FASTEMBED, model="test-model")
    rust, _ = memory.add(
        content="Use Rust for the parser",
        memory_type=MemoryType.DECISION,
        scope=MemoryScope.WORKSPACE,
    )
    memory.add(
        content="Use Python for scripts",
        memory_type=MemoryType.DECISION,
        scope=MemoryScope.WORKSPACE,
    )
    hits = memory.recall("rust", run_id="semantic")
    assert hits[0].memory.id == rust.id
    assert hits[0].semantic_rank == 1
    packed = pack_vector([1.0, 0.0, 0.5])
    assert unpack_vector(packed, 3) == pytest.approx([1.0, 0.0, 0.5])
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1)
    store.close()


def test_local_http_embedding_endpoint_rejects_non_loopback() -> None:
    assert validate_loopback_endpoint("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_endpoint("https://example.com/v1")
    with pytest.raises(ValueError, match="credentials"):
        validate_loopback_endpoint("http://user:secret@localhost/v1")
