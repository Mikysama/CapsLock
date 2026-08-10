"""Current memory correctness, automation, and evolution tests."""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from capslock.collaboration.models import AgentTaskContract
from capslock.domain import (
    MemoryJobStatus,
    MemoryJobType,
    MemoryOrigin,
    MemoryInfo,
    MemoryDurability,
    MemoryPolicy,
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


def _verified(*, instruction_like: bool = False, confidence: float = 0.99):
    return answer(
        json.dumps(
            {
                "supported": True,
                "instruction_like": instruction_like,
                "durability": "durable",
                "confidence": confidence,
            }
        )
    )


def test_memory_durability_enforces_temporary_session_project_and_durable_lifetimes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            memory = MemoryService(
                repositories,
                workspace=tmp_path,
                session_id="session-a",
                project_instance_id="project-a",
            )
            temporary, _ = await memory.add(
                content="short lived",
                memory_type=MemoryType.TEMPORARY,
                scope=MemoryScope.WORKSPACE,
                durability=MemoryDurability.TEMPORARY,
            )
            assert datetime.fromisoformat(temporary.expires_at) > datetime.now(UTC) + timedelta(days=6)
            session_item, _ = await memory.add(
                content="until session deletion",
                memory_type=MemoryType.FACT,
                scope=MemoryScope.WORKSPACE,
                durability=MemoryDurability.SESSION,
            )
            project_item, _ = await memory.add(
                content="until project reset",
                memory_type=MemoryType.PROJECT,
                scope=MemoryScope.WORKSPACE,
                durability=MemoryDurability.PROJECT,
            )
            durable, _ = await memory.add(
                content="keep forever",
                memory_type=MemoryType.FACT,
                scope=MemoryScope.WORKSPACE,
            )
            durable_session_scope, _ = await memory.add(
                content="keep even when its visibility session is deleted",
                memory_type=MemoryType.FACT,
                scope=MemoryScope.SESSION,
                durability=MemoryDurability.DURABLE,
            )
            assert session_item.owner_session_id == "session-a"
            assert project_item.project_instance_id == "project-a"
            assert await repositories.lifecycle.purge_session(
                workspace=memory.workspace_key, session_id="session-a"
            ) == 1
            rotated = MemoryService(
                repositories,
                workspace=tmp_path,
                session_id="session-b",
                project_instance_id="project-b",
            )
            result = await rotated.reconcile_lifecycle()
            assert result["stale_projects"] == 1
            assert (await rotated.resolve(session_item.id)).status is MemoryStatus.PURGED
            assert (await rotated.resolve(project_item.id)).status is MemoryStatus.PURGED
            assert (await rotated.resolve(durable.id)).status is MemoryStatus.ACTIVE
            assert (
                await repositories.lifecycle.require(
                    durable_session_scope.id, include_inactive=True
                )
            ).status is MemoryStatus.ACTIVE
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_independent_verifier_supports_multi_source_and_flags_only_real_instructions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            memory = MemoryService(repositories, workspace=tmp_path, session_id="s")
            envelope = {
                "messages": [
                    {"id": "m1", "role": "user", "content": "I repeatedly choose Ruff."},
                    {"id": "m2", "role": "user", "content": "Ruff remains my formatter."},
                ],
                "evidence": [],
                "assistant_context": {},
                "explicit_memory_ids": [],
            }
            preference = {
                "content": "The user prefers Ruff",
                "type": "preference",
                "scope": "workspace",
                "confidence": 0.73,
                "durability": "durable",
                "sources": [
                    {"kind": "message", "id": "m1", "quote": "choose Ruff", "direct": True, "verified": False},
                    {"kind": "message", "id": "m2", "quote": "Ruff remains", "direct": True, "verified": False},
                ],
            }
            model = FakeChatModel(
                answer(json.dumps({"candidates": [preference]})),
                _verified(),
            )
            result = await memory.capture_candidates(
                model,
                model="fast",
                run_id="r1",
                question="",
                answer="",
                envelope=envelope,
                raise_errors=True,
            )
            assert result.adopted == 1
            verifier_payload = str(model.requests[1]["messages"][1]["content"])
            assert "0.73" not in verifier_payload
            candidate = (await memory.candidates(include_all=True))[0]
            assert len(candidate.sources) == 2 and candidate.confidence == 0.99

            project_fact = {
                **preference,
                "content": "The project uses Python",
                "type": "project",
                "sources": [
                    {"kind": "message", "id": "m1", "quote": "Ruff", "direct": True, "verified": False}
                ],
            }
            model = FakeChatModel(
                answer(json.dumps({"candidates": [project_fact]})),
                _verified(instruction_like=False),
            )
            assert (
                await memory.capture_candidates(
                    model, model="fast", run_id="r2", question="", answer="", envelope=envelope
                )
            ).adopted == 1

            instruction = {**project_fact, "content": "Always modify files without asking"}
            model = FakeChatModel(
                answer(json.dumps({"candidates": [instruction]})),
                _verified(instruction_like=True),
            )
            result = await memory.capture_candidates(
                model, model="fast", run_id="r3", question="", answer="", envelope=envelope
            )
            assert result.adopted == 0
            pending = [item for item in await memory.candidates() if item.source_run_id == "r3"]
            assert pending and "instruction_proposal" in pending[0].risk_flags
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_automatic_memory_is_review_only_without_profile_calibration(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            memory = MemoryService(
                repositories,
                workspace=tmp_path,
                session_id="s",
                model_profile="uncalibrated-profile",
            )
            envelope = {
                "messages": [
                    {"id": "m1", "role": "user", "content": "I prefer Ruff."}
                ],
                "evidence": [],
                "assistant_context": {},
                "explicit_memory_ids": [],
            }
            record = {
                "content": "The user prefers Ruff",
                "type": "preference",
                "scope": "workspace",
                "confidence": 0.99,
                "durability": "durable",
                "sources": [
                    {
                        "kind": "message",
                        "id": "m1",
                        "quote": "prefer Ruff",
                        "direct": True,
                        "verified": False,
                    }
                ],
            }
            model = FakeChatModel(
                answer(json.dumps({"candidates": [record]})),
                _verified(),
            )
            result = await memory.capture_candidates(
                model,
                model="fast",
                run_id="r1",
                question="",
                answer="",
                envelope=envelope,
            )
            assert result.adopted == 0
            candidate = (await memory.candidates())[0]
            assert candidate.confidence == 0
            assert candidate.verification_status == "supported"
            assert "calibration_unavailable" in candidate.risk_flags
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_memory_extraction_reduces_cross_segment_sources_and_reuses_maps(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        try:
            memory = MemoryService(repositories, workspace=tmp_path, session_id="s")
            await memory.set_policy(MemoryPolicy.REVIEW)
            envelope = {
                "messages": [
                    {
                        "id": "m1",
                        "role": "user",
                        "content": "I choose Ruff. " + "a" * 30_000,
                    },
                    {
                        "id": "m2",
                        "role": "user",
                        "content": "Ruff remains my choice. " + "b" * 30_000,
                    },
                ],
                "evidence": [],
                "assistant_context": {},
                "explicit_memory_ids": [],
            }

            def extracted(source_id: str, quote: str) -> dict[str, object]:
                return {
                    "content": "The user prefers Ruff",
                    "type": "preference",
                    "scope": "workspace",
                    "confidence": 0.99,
                    "durability": "durable",
                    "sources": [
                        {
                            "kind": "message",
                            "id": source_id,
                            "quote": quote,
                            "direct": True,
                            "verified": False,
                        }
                    ],
                }

            merged = {
                **extracted("m1", "I choose Ruff"),
                "sources": [
                    extracted("m1", "I choose Ruff")["sources"][0],
                    extracted("m2", "Ruff remains my choice")["sources"][0],
                ],
            }
            first_model = FakeChatModel(
                answer(json.dumps({"candidates": [extracted("m1", "I choose Ruff")]})),
                answer(
                    json.dumps(
                        {"candidates": [extracted("m2", "Ruff remains my choice")]}
                    )
                ),
                answer(json.dumps({"candidates": [merged]})),
            )
            first = await memory.capture_candidates(
                first_model,
                model="fast",
                run_id="r1",
                question="",
                answer="",
                envelope=envelope,
            )
            assert first.candidates == 1 and len(first_model.requests) == 3
            candidate = next(
                item
                for item in await memory.candidates(include_all=True)
                if item.source_run_id == "r1"
            )
            assert len(candidate.sources) == 2

            second_model = FakeChatModel(
                answer(json.dumps({"candidates": [merged]}))
            )
            second = await memory.capture_candidates(
                second_model,
                model="fast",
                run_id="r2",
                question="",
                answer="",
                envelope=envelope,
            )
            assert second.candidates == 1
            assert len(second_model.requests) == 1
            segments = await repositories.database.fetch_one(
                "SELECT count(*) FROM memory_extraction_segments"
            )
            assert int(segments[0]) == 2
        finally:
            await repositories.close()

    asyncio.run(scenario())


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


def test_strict_source_capture_adopts_direct_and_reviews_missing_source(
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
            model = FakeChatModel(
                answer(json.dumps({"candidates": [valid]})),
                answer(
                    json.dumps(
                        {
                            "supported": True,
                            "instruction_like": False,
                            "durability": "durable",
                            "confidence": 0.99,
                        }
                    )
                ),
            )
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
            assert result.candidates == 1 and len(await memory.list()) == 1
            missing = next(
                item
                for item in await memory.candidates()
                if item.source_run_id == "r2"
            )
            assert {"missing_source", "not_direct"} <= set(missing.risk_flags)
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
