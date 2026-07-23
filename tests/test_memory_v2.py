from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from capslock.domain import (
    EmbeddingBackend,
    MemoryCandidateStatus,
    MemoryOrigin,
    MemoryScope,
    MemoryStatus,
    MemoryType,
)
from capslock.memory import MemoryService
from capslock.memory.transfer import EXPORT_FORMAT, EXPORT_VERSION
from capslock.storage.memory_repositories import MemoryRepositories
from tests.helpers import FakeChatModel, answer


async def create_memory(
    repositories: MemoryRepositories,
    *,
    content: str,
    scope: MemoryScope = MemoryScope.WORKSPACE,
    workspace: str = "workspace",
    session_id: str = "session",
    expires_at: str | None = None,
):
    return await repositories.lifecycle.create(
        content=content,
        memory_type=MemoryType.FACT,
        scope=scope,
        workspace=None if scope is MemoryScope.GLOBAL else workspace,
        session_id=session_id if scope is MemoryScope.SESSION else None,
        source_kind="manual",
        source_ref=session_id,
        confidence=0.9,
        expires_at=expires_at,
        origin=MemoryOrigin.MANUAL,
    )


def test_immutable_revisions_expiry_clear_forget_and_undo(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            item = await create_memory(
                repositories,
                content="first",
                expires_at="2030-01-01T00:00:00+00:00",
            )
            edited = await repositories.lifecycle.edit(
                item.id,
                content="second",
                memory_type=MemoryType.DECISION,
                source_kind="manual",
                source_ref="session",
                confidence=1,
                expires_at=None,
            )
            assert edited.revision == 2
            assert edited.expires_at is None
            forgotten = await repositories.lifecycle.forget(item.id)
            assert forgotten.status is MemoryStatus.FORGOTTEN
            assert forgotten.revision == 3
            restored = await repositories.lifecycle.undo(item.id)
            assert restored.status is MemoryStatus.ACTIVE
            assert restored.content == "second"
            assert restored.type is MemoryType.DECISION
            assert restored.expires_at is None
            rows = await repositories.database.fetch_all(
                "SELECT revision,operation,content FROM memory_revisions WHERE memory_id=? ORDER BY revision",
                (item.id,),
            )
            assert [(row[0], row[1]) for row in rows] == [
                (1, "create"),
                (2, "edit"),
                (3, "forget"),
                (4, "undo"),
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_purge_removes_content_sources_fts_and_vectors_but_retains_identity_audit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            item = await create_memory(repositories, content="sensitive text")
            await repositories.embeddings.put(
                item,
                backend=EmbeddingBackend.FASTEMBED,
                model="test",
                dimensions=1,
                vector=b"1234",
                content_hash="hash",
            )
            purged = await repositories.lifecycle.purge(item.id)
            assert purged.status is MemoryStatus.PURGED
            assert purged.content is None and purged.revision == 0
            for table in (
                "memory_revisions",
                "memory_sources",
                "memory_embeddings",
                "memory_fts",
            ):
                assert (
                    await repositories.database.fetch_one(
                        f"SELECT count(*) FROM {table} WHERE memory_id=?", (item.id,)
                    )
                )[0] == 0
            identity = await repositories.database.fetch_one(
                "SELECT status,current_revision FROM memories WHERE id=?", (item.id,)
            )
            assert tuple(identity) == ("purged", None)
            assert (
                await repositories.database.fetch_one(
                    "SELECT count(*) FROM memory_audit WHERE memory_id=? AND operation='purge'",
                    (item.id,),
                )
            )[0] == 1
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_scope_visibility_isolated_by_workspace_and_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            global_item = await create_memory(
                repositories, content="global", scope=MemoryScope.GLOBAL
            )
            workspace_item = await create_memory(
                repositories, content="workspace", workspace="a"
            )
            session_item = await create_memory(
                repositories,
                content="session",
                scope=MemoryScope.SESSION,
                workspace="a",
                session_id="one",
            )
            visible = await repositories.query.list_visible(
                workspace="a", session_id="one"
            )
            assert {item.id for item in visible} == {
                global_item.id,
                workspace_item.id,
                session_item.id,
            }
            other_session = await repositories.query.list_visible(
                workspace="a", session_id="two"
            )
            assert {item.id for item in other_session} == {
                global_item.id,
                workspace_item.id,
            }
            other_workspace = await repositories.query.list_visible(
                workspace="b", session_id="one"
            )
            assert {item.id for item in other_workspace} == {global_item.id}
        finally:
            await repositories.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("content", "query"),
    [
        ("I prefer Black for Python formatting", "What formatter do I prefer, Black?"),
        ("我偏好高对比度终端主题", "我偏好哪种终端主题？"),
    ],
)
def test_fts_query_normalization_handles_natural_language_and_chinese_fallback(
    tmp_path: Path, content: str, query: str
) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(
            tmp_path / (str(abs(hash(content))) + ".sqlite3")
        )
        try:
            item = await create_memory(repositories, content=content)
            matches = await repositories.query.search(
                query, workspace="workspace", session_id="session"
            )
            assert [match.id for match in matches] == [item.id]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_lexical_recall_is_bounded_and_embedding_failure_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            service = MemoryService(
                repositories, workspace=tmp_path, session_id="session"
            )
            for index in range(7):
                await service.add(
                    content=f"project codename atlas detail {index}",
                    memory_type=MemoryType.FACT,
                    scope=MemoryScope.SESSION,
                )

            async def fail_semantic(*args, **kwargs):
                raise RuntimeError("embedding unavailable")

            monkeypatch.setattr(service.embeddings, "semantic_ranks", fail_semantic)
            text, hits = await service.recall_context("atlas", run_id="run")
            assert len(hits) == 5
            assert sum(len((hit.memory.content or "").encode()) for hit in hits) <= 4096
            assert "untrusted-memory-context-json" in text
            assert len(await service.context("run")) == 5
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_candidate_extraction_review_and_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            service = MemoryService(
                repositories, workspace=tmp_path, session_id="session"
            )
            model = FakeChatModel(
                answer(
                    json.dumps(
                        {
                            "candidates": [
                                {
                                    "content": "Use tabs for generated reports",
                                    "type": "preference",
                                    "scope": "workspace",
                                    "confidence": 0.95,
                                    "direct": True,
                                }
                            ]
                        }
                    ),
                    input_tokens=4,
                    output_tokens=3,
                )
            )
            result = await service.capture_candidates(
                model,
                model="test",
                run_id="run",
                question="I prefer tabs",
                answer="Understood",
            )
            assert result.candidates == 1
            candidate = (await service.candidates())[0]
            assert candidate.status is MemoryCandidateStatus.PENDING
            adopted = await service.accept_candidate(candidate.id)
            assert adopted.origin is MemoryOrigin.REVIEWED
            decided = await service.resolve_candidate(candidate.id)
            assert decided.status is MemoryCandidateStatus.ACCEPTED
            assert decided.content is None
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_memory_export_v3_round_trip_and_old_versions_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            service = MemoryService(
                repositories, workspace=tmp_path, session_id="session"
            )
            await service.add(
                content="Exported setting",
                memory_type=MemoryType.NOTE,
                scope=MemoryScope.WORKSPACE,
            )
            (tmp_path / "exports").mkdir()
            path, count = await service.export_json(
                MemoryScope.WORKSPACE, "exports/memory.json"
            )
            document = json.loads(path.read_text(encoding="utf-8"))
            assert (document["format"], document["version"], count) == (
                EXPORT_FORMAT,
                EXPORT_VERSION,
                1,
            )
            imported, _ = await service.import_json(
                MemoryScope.SESSION, "exports/memory.json"
            )
            assert len(imported) == 1
            assert imported[0].scope is MemoryScope.SESSION

            old = tmp_path / "exports" / "old.json"
            old.write_text(
                json.dumps({"format": EXPORT_FORMAT, "version": 2, "records": []}),
                encoding="utf-8",
            )
            with pytest.raises(ValueError, match="version 3"):
                await service.import_json(MemoryScope.WORKSPACE, "exports/old.json")
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_memory_database_file_permissions(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "memory.sqlite3"
        repositories = await MemoryRepositories.open(path)
        await repositories.close()
        assert path.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())
