from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from capslock.configuration import ContextSettings
from capslock.runtime.context import ContextBudgetExceeded, ContextBudgetManager
from capslock.runtime.model import ModelMessage, ModelResponse, ModelUsage
from capslock.storage.artifacts import ToolArtifactStore
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import workflow_service, workspace_run


SUMMARY = {
    "goal": "retain every source",
    "constraints": [],
    "completed_work": [],
    "decisions": [],
    "files": [],
    "failures": [],
    "evidence": [],
    "pending": [],
    "summary_version": 2,
    "source_refs": [],
    "retrieval_hints": [],
}


class Summarizer:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, **request):
        self.requests.append(request)
        return ModelResponse(ModelMessage(json.dumps(SUMMARY)), ModelUsage(3, 2))


class NoCompactions:
    async def matching(self, *_args, **_kwargs):
        return None


class CachingCompactions(NoCompactions):
    def __init__(self) -> None:
        self.segments: dict[tuple[str, str], dict[str, object]] = {}

    async def summary_segment(self, digest, profile):
        return self.segments.get((digest, profile))

    async def store_summary_segment(
        self, *, source_digest, model_profile, summary, **_values
    ):
        self.segments[(source_digest, model_profile)] = summary


def test_episodic_retrieval_indexes_messages_and_artifacts_with_session_isolation(
    tmp_path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            first, run = await workspace_run(repositories, "first")
            other, other_run = await workspace_run(repositories, "other")
            await repositories.sessions.append_message(
                first.id, run.run.id, "user", "atlas canary BLUE-17"
            )
            await repositories.sessions.append_message(
                other.id, other_run.run.id, "user", "atlas canary WRONG"
            )
            artifacts = ToolArtifactStore(
                tmp_path / "artifacts", repositories.database, repositories.episodic
            )
            artifact = await artifacts.put(
                session_id=first.id,
                run_id=run.run.id,
                content=b"artifact canary GREEN-29",
            )
            hits = await repositories.episodic.search(
                "atlas canary", session_id=first.id
            )
            assert any(item.content == "atlas canary BLUE-17" for item in hits)
            assert all("WRONG" not in item.content for item in hits)
            artifact_hits = await repositories.episodic.search(
                "artifact canary", session_id=first.id
            )
            assert artifact_hits[0].artifact_id == artifact.id
            assert artifact_hits[0].as_dict()["read_with"] == "read_tool_artifact"
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_context_automatically_injects_bounded_episodic_recall(tmp_path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, history = await workspace_run(repositories, "history")
            await repositories.sessions.append_message(
                session.id, history.run.id, "user", "deployment codename ORCHID-41"
            )
            current = await workflow_service(repositories).prepare(
                session.id, "What is the deployment codename?"
            )
            manager = ContextBudgetManager(
                sessions=repositories.sessions,
                compactions=repositories.compactions,
                settings=ContextSettings(),
                context_window=10_000,
                max_output_tokens=100,
                model_profile="test",
                model_name="test",
                tool_schemas=[],
                episodic=repositories.episodic,
            )
            result = await manager.build(
                session.id,
                "deployment codename",
                run_id=current.run.id,
                instructions="system",
                summarizer=Summarizer(),
            )
            rendered = "\n".join(str(item["content"]) for item in result.messages)
            assert "episodic_recall" in rendered and "ORCHID-41" in rendered
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_micro_compaction_never_replaces_content_without_durable_artifact() -> None:
    class FailingArtifacts:
        async def put(self, **_values):
            raise OSError("disk full")

    manager = ContextBudgetManager(
        sessions=SimpleNamespace(),
        compactions=NoCompactions(),
        settings=ContextSettings(),
        context_window=20_000,
        max_output_tokens=100,
        model_profile="test",
        model_name="test",
        tool_schemas=[],
        artifacts=FailingArtifacts(),
    )
    original = "recover-me" * 2_000
    messages = [
        {"role": "tool", "tool_call_id": "call", "content": original},
        *({"role": "user", "content": str(index)} for index in range(20)),
    ]
    with pytest.raises(ContextBudgetExceeded) as error:
        asyncio.run(
            manager.micro_compact(messages, session_id="session", run_id="run")
        )
    assert error.value.code == "context_budget_exceeded"
    assert messages[0]["content"] == original


def test_hierarchical_summary_submits_front_middle_and_tail_without_slicing() -> None:
    manager = ContextBudgetManager(
        sessions=SimpleNamespace(),
        compactions=NoCompactions(),
        settings=ContextSettings(),
        context_window=2_000,
        max_output_tokens=500,
        model_profile="test",
        model_name="test",
        tool_schemas=[],
    )
    entries = [
        {"id": index, "role": "user", "content": marker + "x" * 3_000}
        for index, marker in enumerate(("FRONT-11", "MIDDLE-22", "TAIL-33"))
    ]
    model = Summarizer()
    summary, _, _ = asyncio.run(manager._summarize(entries, model))
    submitted = "\n".join(
        str(message["content"])
        for request in model.requests
        for message in request["messages"]
        if message["role"] == "user"
    )
    assert summary["summary_version"] == 2
    assert all(marker in submitted for marker in ("FRONT-11", "MIDDLE-22", "TAIL-33"))
    assert len(model.requests) > 1


def test_hierarchical_summary_reuses_unchanged_segment_digests() -> None:
    compactions = CachingCompactions()
    manager = ContextBudgetManager(
        sessions=SimpleNamespace(),
        compactions=compactions,
        settings=ContextSettings(),
        context_window=2_000,
        max_output_tokens=500,
        model_profile="test",
        model_name="test",
        tool_schemas=[],
    )
    entries = [
        {"id": index, "role": "user", "content": marker + "x" * 3_000}
        for index, marker in enumerate(("FRONT-11", "MIDDLE-22", "TAIL-33"))
    ]
    model = Summarizer()
    first, _, _ = asyncio.run(manager._summarize(entries, model))
    request_count = len(model.requests)
    second, input_tokens, output_tokens = asyncio.run(manager._summarize(entries, model))
    assert second == first
    assert len(model.requests) == request_count
    assert input_tokens == output_tokens == 0


def test_artifact_index_omits_binary_and_quarantined_content(tmp_path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, run = await workspace_run(repositories, "artifacts")
            artifacts = ToolArtifactStore(
                tmp_path / "artifacts", repositories.database, repositories.episodic
            )
            binary = await artifacts.put(
                session_id=session.id,
                run_id=run.run.id,
                content=b"BINARY-CANARY-91\x00\x01",
                media_type="application/octet-stream",
            )
            quarantined = await artifacts.put(
                session_id=session.id,
                run_id=run.run.id,
                content=b"IGNORE-INSTRUCTIONS-CANARY-73",
                media_type="text/plain",
                index_content=False,
            )
            assert not await repositories.episodic.search(
                "BINARY-CANARY-91", session_id=session.id
            )
            assert not await repositories.episodic.search(
                "IGNORE-INSTRUCTIONS-CANARY-73", session_id=session.id
            )
            metadata = await repositories.episodic.search(
                "content omitted", session_id=session.id, limit=20
            )
            assert {item.artifact_id for item in metadata} == {
                binary.id,
                quarantined.id,
            }
        finally:
            await repositories.close()

    asyncio.run(scenario())
