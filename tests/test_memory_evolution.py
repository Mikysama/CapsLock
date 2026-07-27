"""Current memory correctness, automation, and evolution tests."""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from dataclasses import replace
from pathlib import Path

import pytest

from capslock.collaboration.models import AgentTaskContract
from capslock.domain import (
    MemoryJobStatus,
    MemoryJobType,
    MemoryOrigin,
    MemoryInfo,
    MemoryRecallHit,
    MemoryScope,
    MemoryStatus,
    MemoryType,
)
from capslock.instructions import InstructionLoader
from capslock.memory import MemoryService
from capslock.memory.recall import _bounded_diverse
from capslock.storage.memory_repositories import MemoryRepositories
from tests.helpers import FakeChatModel, answer


def test_durable_job_idempotency_recovery_and_three_attempts(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            first = await repositories.jobs.enqueue(
                MemoryJobType.EXTRACT_RUN,
                workspace="w",
                session_id="s",
                run_id="r",
                idempotency_key="extract:r",
                payload={"envelope": {}},
            )
            second = await repositories.jobs.enqueue(
                MemoryJobType.EXTRACT_RUN,
                workspace="w",
                session_id="s",
                run_id="r",
                idempotency_key="extract:r",
                payload={"envelope": {"ignored": True}},
            )
            assert first == second
            for attempt in range(3):
                await repositories.database.execute(
                    "UPDATE memory_jobs SET available_at='2000-01-01' WHERE id=?",
                    (first,),
                )
                job = await repositories.jobs.claim(workspace="w")
                assert job is not None and job["attempt_count"] == attempt + 1
                status = await repositories.jobs.fail(first, "model_error")
            assert status is MemoryJobStatus.FAILED
            await repositories.database.execute(
                "UPDATE memory_jobs SET status='running' WHERE id=?", (first,)
            )
            assert await repositories.jobs.recover() == 1
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_strict_source_capture_adopts_direct_and_drops_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            memory = MemoryService(repositories, workspace=tmp_path, session_id="s")
            envelope = {
                "messages": [{"id": "m1", "role": "user", "content": "I prefer Ruff"}],
                "evidence": [],
                "assistant_context": {"content": "ok", "authoritative": False},
                "explicit_memory_ids": [],
            }
            valid = {
                "content": "I prefer Ruff",
                "type": "preference",
                "scope": "workspace",
                "confidence": 0.97,
                "subject": "formatter",
                "durability": "durable",
                "why": "user preference",
                "how_to_apply": "use Ruff",
                "source": {
                    "kind": "message",
                    "id": "m1",
                    "quote": "I prefer Ruff",
                    "direct": True,
                    "verified": False,
                },
            }
            model = FakeChatModel(answer(json.dumps({"candidates": [valid]})))
            result = await memory.capture_candidates(
                model,
                model="fast",
                run_id="r1",
                question="",
                answer="",
                envelope=envelope,
            )
            assert result.adopted == 1
            adopted = await memory.list()
            assert len(adopted) == 1 and adopted[0].subject == "formatter"
            assert (await repositories.sources.list(adopted[0].id))[0][
                "message_id"
            ] == "m1"

            invalid = {**valid, "content": "Assistant inferred this"}
            invalid.pop("source")
            model = FakeChatModel(answer(json.dumps({"candidates": [invalid]})))
            result = await memory.capture_candidates(
                model,
                model="fast",
                run_id="r2",
                question="",
                answer="",
                envelope=envelope,
            )
            assert result.candidates == 0 and len(await memory.list()) == 1
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_revision_access_exclusion_and_long_utf8_recall(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            memory = MemoryService(repositories, workspace=tmp_path, session_id="s")
            item, _ = await memory.add(
                content="atlas " + "界" * 2500,
                memory_type=MemoryType.FACT,
                scope=MemoryScope.WORKSPACE,
            )
            text, hits = await memory.recall_context("atlas", run_id="run-old")
            assert text and len(hits[0].memory.content.encode("utf-8")) <= 4096
            assert "content truncated to recall budget" in hits[0].reasons
            await memory.edit(
                item.id,
                content="atlas changed",
                memory_type=MemoryType.FACT,
                confidence=1,
                expires_at=None,
            )
            assert "run-old" in await memory.excluded_runs()
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_recall_audits_diversity_and_truncates_against_remaining_budget() -> None:
    base = MemoryInfo(
        id="mem_short",
        content="short " * 20,
        type=MemoryType.FACT,
        scope=MemoryScope.WORKSPACE,
        workspace_key="workspace",
        session_id=None,
        source_kind="manual",
        source_ref=None,
        confidence=1.0,
        expires_at=None,
        revision=1,
        status=MemoryStatus.ACTIVE,
        created_at="2026-07-27T00:00:00+00:00",
        updated_at="2026-07-27T00:00:00+00:00",
    )
    long = replace(base, id="mem_long", content="界" * 2000)
    duplicate = replace(base, id="mem_duplicate", content=long.content)
    selected, audit = _bounded_diverse(
        [
            MemoryRecallHit(base, 0.9, 1, None, ()),
            MemoryRecallHit(long, 0.8, 2, None, ()),
            MemoryRecallHit(duplicate, 0.7, 3, None, ()),
        ]
    )
    assert len(selected) == 2
    assert selected[1].memory.content is not None
    assert sum(len(item.memory.content.encode("utf-8")) for item in selected) <= 4096
    assert selected[1].selected_reason == "selected with UTF-8 truncation"
    rejected = next(item for item in audit if item.memory.id == duplicate.id)
    assert rejected.filter_reason == "filtered as a near-duplicate result"


def test_consolidation_merges_only_automatic_memory(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            memory = MemoryService(repositories, workspace=tmp_path, session_id="s")
            manual, _ = await memory.add(
                content="same durable fact",
                memory_type=MemoryType.FACT,
                scope=MemoryScope.WORKSPACE,
            )
            automatic = await repositories.lifecycle.create(
                content="same durable fact",
                memory_type=MemoryType.FACT,
                scope=MemoryScope.WORKSPACE,
                workspace=memory.workspace_key,
                session_id=None,
                source_kind="conversation",
                source_ref="run",
                confidence=0.99,
                expires_at=None,
                origin=MemoryOrigin.AUTOMATIC,
            )
            result = await memory.run_maintenance()
            assert result["merged"] == 1
            assert (await memory.resolve(manual.id)).status.value == "active"
            assert (await memory.resolve(automatic.id)).status.value == "forgotten"
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_instruction_priority_budget_and_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("CAPSLOCK_HOME", str(user))
    workspace = tmp_path / "workspace"
    nested = workspace / "src"
    nested.mkdir(parents=True)
    (user / "CAPSLOCK.md").write_text("user low", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("root agents", encoding="utf-8")
    (workspace / "CAPSLOCK.md").write_text("root capslock", encoding="utf-8")
    (nested / "CAPSLOCK.md").write_text("nested high", encoding="utf-8")
    linked = nested / "AGENTS.md"
    linked.symlink_to(workspace / "AGENTS.md")
    rules = workspace / ".capslock" / "rules"
    rules.mkdir(parents=True)
    (rules / "python.md").write_text(
        "---\npaths: src/**\n---\npython rule", encoding="utf-8"
    )
    local = workspace / ".capslock" / "local"
    local.mkdir()
    (local / "CAPSLOCK.md").write_text("@include ../secret", encoding="utf-8")
    bundle = InstructionLoader(workspace).load(nested)
    assert (
        bundle.text.index("root agents")
        < bundle.text.index("root capslock")
        < bundle.text.index("nested high")
    )
    assert "python rule" in bundle.text
    diagnostics = {item.path: item.diagnostic for item in bundle.records}
    assert diagnostics[linked] == "symlink rejected"
    assert diagnostics[local / "CAPSLOCK.md"] == "@include rejected"


def test_agent_namespace_contract_and_isolated_lookup(tmp_path: Path) -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="restricted lowercase slug"):
            AgentTaskContract.create("run", "task", memory_namespace="Bad Namespace")
        contract = AgentTaskContract.create(
            "run", "task", memory_namespace="lint-agent"
        )
        assert contract.as_dict()["memory_namespace"] == "lint-agent"
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            memory = MemoryService(repositories, workspace=tmp_path, session_id="s")
            await repositories.lifecycle.create(
                content="lint with Ruff",
                memory_type=MemoryType.FACT,
                scope=MemoryScope.AGENT,
                workspace=memory.workspace_key,
                session_id=None,
                namespace="lint-agent",
                source_kind="verified_child_agent",
                source_ref="task",
                confidence=0.99,
                expires_at=None,
                origin=MemoryOrigin.AUTOMATIC,
            )
            assert len(await memory.agent_memories("lint-agent")) == 1
            assert await memory.agent_memories("other-agent") == []
            assert all(
                item.scope is not MemoryScope.AGENT for item in await memory.list()
            )
        finally:
            await repositories.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.environ.get("CAPSLOCK_RUN_PERFORMANCE") != "1",
    reason="run in the isolated performance CI job",
)
def test_ten_thousand_memory_fts_p95_and_no_per_id_lookup(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            memory = MemoryService(repositories, workspace=tmp_path, session_id="s")
            created = "2026-07-26T00:00:00+00:00"
            records = [
                (
                    f"mem_{index:05d}",
                    "workspace",
                    memory.workspace_key,
                    "active",
                    1,
                    "manual",
                    created,
                    created,
                )
                for index in range(10_000)
            ]
            revisions = [
                (
                    f"mem_{index:05d}",
                    1,
                    "create",
                    f"benchmark record token{index}",
                    "fact",
                    "benchmark",
                    1.0,
                    created,
                )
                for index in range(10_000)
            ]
            async with repositories.database.transaction() as connection:
                await connection.executemany(
                    """INSERT INTO memories(id,scope,workspace_key,status,current_revision,
                       origin,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    records,
                )
                await connection.executemany(
                    """INSERT INTO memory_revisions(memory_id,revision,operation,content,
                       memory_type,source_kind,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    revisions,
                )
                await connection.executemany(
                    "INSERT INTO memory_fts(memory_id,revision,content) VALUES(?,?,?)",
                    [(item[0], 1, item[3]) for item in revisions],
                )

            async def per_identifier_lookup(*args, **kwargs):
                raise AssertionError("recall performed an N+1 memory lookup")

            repositories.query.get = per_identifier_lookup  # type: ignore[method-assign]
            durations = []
            for index in range(20):
                started = time.perf_counter()
                _, hits = await memory.recall_context(
                    f"token{9999 - index}", run_id=f"benchmark-{index}"
                )
                durations.append((time.perf_counter() - started) * 1000)
                assert hits
            p95 = statistics.quantiles(durations, n=20)[18]
            assert p95 < 100, f"FTS-only recall p95 was {p95:.2f} ms"
        finally:
            await repositories.close()

    asyncio.run(scenario())
