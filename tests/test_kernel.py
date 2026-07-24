"""Run-kernel, artifact, event, and capability boundary tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from capslock.configuration import ContextSettings
from capslock.domain import AgentEventKind, WorkItemStatus
from capslock.plugins.broker import BrokerCallbacks, HostCapabilityBroker
from capslock.plugins.manifest import PluginCapabilities
from capslock.policy import PolicyError, WorkspacePolicy
from capslock.runtime import RunEngine, RunRequest
from capslock.runtime.context import ContextBudgetManager
from capslock.runtime.events import RunEventBus
from capslock.runtime.model import ModelMessage, ModelResponse, ModelToolCall
from capslock.runtime.tool_loop import ToolLoop
from capslock.storage.artifacts import ArtifactAccessError, ToolArtifactStore
from capslock.storage.repositories import WorkspaceRepositories
from capslock.tooling import (
    ExecutionContext,
    InterruptBehavior,
    ResolvedToolPolicy,
    ToolRuntime,
    ToolOutcome,
    define_tool,
)
from tests.helpers import FakeChatModel, answer, workflow_service, workspace_run

ToolRegistry = ToolRuntime
ToolCapabilities = ResolvedToolPolicy


def Tool(name, description, schema, execute, *, capabilities=ResolvedToolPolicy()):
    return define_tool(name, description, schema, execute, policy=capabilities)


def ToolResult(ok, data, error=None):
    return (
        ToolOutcome.success(data)
        if ok
        else ToolOutcome.failure(error or "failed", data=data)
    )


def test_run_engine_serializes_requests() -> None:
    async def scenario() -> None:
        active = 0
        peak = 0

        async def execute(question: str, **values) -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

        engine = RunEngine(execute)

        async def consume(question: str) -> None:
            async for _ in engine.run_stream(RunRequest(question=question)):
                pass

        await asyncio.gather(consume("first"), consume("second"))
        assert peak == 1
        assert not engine.active

    asyncio.run(scenario())


def test_read_only_tools_run_concurrently_and_commit_in_call_order(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            both_started = asyncio.Event()
            started: set[str] = set()

            async def read(context, arguments):
                name = str(arguments["name"])
                started.add(name)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=1)
                return ToolResult(True, {"name": name})

            capabilities = ToolCapabilities(read_only=True, concurrency_safe=True)
            tools = ToolRegistry(
                [
                    Tool(
                        "read",
                        "read",
                        {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                        read,
                        capabilities=capabilities,
                    )
                ]
            )
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (
                            ModelToolCall("first", "read", '{"name":"a"}'),
                            ModelToolCall("second", "read", '{"name":"b"}'),
                        ),
                    )
                ),
                answer("done"),
            )
            loop = ToolLoop(
                chat_model=model,
                model="test-model",
                tools=tools,
                journal=repositories.run_journal,
                max_tool_rounds=2,
                context_factory=lambda run_id: ExecutionContext(
                    session_id=session.id,
                    run_id=run_id,
                    policy=WorkspacePolicy(tmp_path),
                    event=lambda *args, **kwargs: None,
                    actions=object(),
                ),
            )
            messages = [{"role": "user", "content": "read"}]
            await asyncio.wait_for(
                loop.run(
                    messages,
                    prepared.run.id,
                    emit=lambda kind, data: _nothing(),
                ),
                timeout=10,
            )
            tool_messages = [item for item in messages if item["role"] == "tool"]
            assert [item["tool_call_id"] for item in tool_messages] == [
                "first",
                "second",
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_parallel_fail_fast_cancels_siblings_and_commits_results(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            slow_started = asyncio.Event()
            slow_cancelled = asyncio.Event()

            async def execute(context, arguments):
                if arguments["name"] == "fail":
                    await slow_started.wait()
                    return ToolOutcome.failure("failed deliberately")
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    slow_cancelled.set()
                    raise

            policy = ResolvedToolPolicy(
                read_only=True,
                concurrency_safe=True,
                interrupt_behavior=InterruptBehavior.CANCEL,
                fail_fast=True,
            )
            tools = ToolRuntime(
                [
                    define_tool(
                        "read",
                        "Read concurrently.",
                        {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                        execute,
                        policy=policy,
                    )
                ]
            )
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (
                            ModelToolCall("first", "read", '{"name":"fail"}'),
                            ModelToolCall("second", "read", '{"name":"slow"}'),
                        ),
                    )
                ),
                answer("done"),
            )
            loop = ToolLoop(
                chat_model=model,
                model="test-model",
                tools=tools,
                journal=repositories.run_journal,
                max_tool_rounds=2,
                context_factory=lambda run_id: ExecutionContext(
                    session_id=session.id,
                    run_id=run_id,
                    policy=WorkspacePolicy(tmp_path),
                    event=lambda *args, **kwargs: None,
                    actions=object(),
                ),
            )
            messages = [{"role": "user", "content": "read"}]
            result = await loop.run(
                messages,
                prepared.run.id,
                emit=lambda kind, data: asyncio.sleep(0),
            )
            assert result.text == "done"
            assert slow_cancelled.is_set()
            tool_messages = [item for item in messages if item["role"] == "tool"]
            assert [item["tool_call_id"] for item in tool_messages] == [
                "first",
                "second",
            ]
            assert '"status": "failed"' in tool_messages[0]["content"]
            assert '"status": "cancelled"' in tool_messages[1]["content"]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_tool_artifacts_are_session_scoped_and_integrity_checked(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            store = ToolArtifactStore(tmp_path / "artifacts", repositories.database)
            artifact = await store.put(
                session_id=session.id,
                run_id=prepared.run.id,
                content=b"sensitive result",
            )
            record, chunk, has_more = await store.read(
                artifact.id, session_id=session.id, limit=9
            )
            assert record.sha256 == artifact.sha256
            assert chunk == b"sensitive"
            assert has_more
            with pytest.raises(ArtifactAccessError):
                await store.read(artifact.id, session_id="another-session")

            path = next((tmp_path / "artifacts" / "sha256").rglob(artifact.sha256))
            path.write_bytes(b"tampered")
            with pytest.raises(ArtifactAccessError, match="integrity"):
                await store.read(artifact.id, session_id=session.id)
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_context_compaction_is_structured_and_reused(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, history = await workspace_run(repositories, "history")
            for index in range(10):
                role = "user" if index % 2 == 0 else "assistant"
                await repositories.sessions.append_message(
                    session.id,
                    history.run.id,
                    role,
                    f"message {index}: " + "x" * 100,
                )
            await workflow_service(repositories).finish(
                history.run.id,
                status=WorkItemStatus.COMPLETED,
                event_kind=AgentEventKind.COMPLETED,
                payload={"status": "completed", "answer": "done"},
                duration_ms=1,
            )
            current = await workflow_service(repositories).prepare(
                session.id, "current"
            )
            summary = {
                "goal": "continue",
                "constraints": ["local"],
                "completed_work": ["inspection"],
                "decisions": [],
                "files": [],
                "failures": [],
                "evidence": [],
                "pending": ["answer"],
            }
            summarizer = FakeChatModel(answer(json.dumps(summary)))
            manager = ContextBudgetManager(
                sessions=repositories.sessions,
                compactions=repositories.compactions,
                settings=ContextSettings(
                    trigger_ratio=0.50,
                    target_ratio=0.40,
                    preserve_recent_turns=1,
                ),
                context_window=800,
                max_output_tokens=100,
                model_profile="fast",
                model_name="test-model",
                tool_schemas=[],
            )
            first = await manager.build(
                session.id,
                "current",
                run_id=current.run.id,
                instructions="system",
                summarizer=summarizer,
            )
            second = await manager.build(
                session.id,
                "current",
                run_id=current.run.id,
                instructions="system",
                summarizer=summarizer,
            )
            assert first.compaction_id == second.compaction_id
            assert len(summarizer.requests) == 1
            assert "compaction-summary-json" in str(first.messages[0]["content"])
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_event_bus_orders_events_and_fails_closed_on_durability_error() -> None:
    class Journal:
        def __init__(self) -> None:
            self.events = []
            self.fail = False

        async def event_state(self, run_id):
            return 0, "session", "work", ""

        async def append_prepared_events(self, events):
            if self.fail:
                raise OSError("disk full")
            self.events.extend(events)

    async def scenario() -> None:
        journal = Journal()
        consumed = []
        bus = RunEventBus(
            run_id="run",
            journal=journal,
            consumer=lambda event: _append(consumed, event),
            diagnostic=lambda *args, **kwargs: None,
        )
        first = await bus.emit(AgentEventKind.THINKING, {})
        second = await bus.emit(AgentEventKind.TEXT_DELTA, {"text": "ok"})
        assert [item.sequence for item in consumed] == [1, 2]
        assert first.trace_id == second.trace_id
        assert first.event_id != second.event_id
        await bus.flush()
        assert journal.events == consumed

        journal.fail = True
        await bus.emit(AgentEventKind.THINKING, {})
        with pytest.raises(OSError, match="disk full"):
            await bus.flush()
        with pytest.raises(RuntimeError, match="durable"):
            await bus.emit(AgentEventKind.THINKING, {})

    asyncio.run(scenario())


def test_plugin_broker_enforces_scopes_and_missing_approvals(tmp_path: Path) -> None:
    async def scenario() -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "data.txt").write_text("data", encoding="utf-8")
        broker = HostCapabilityBroker(
            WorkspacePolicy(tmp_path),
            PluginCapabilities(
                workspace_read=("src/**",),
                workspace_write=("src/**",),
                credentials=("MODEL_KEY",),
            ),
        )
        result = await broker.request(
            {"capability": "workspace_read", "path": "src/data.txt"}
        )
        assert result["content"] == "data"
        with pytest.raises(PolicyError, match="outside"):
            await broker.request(
                {"capability": "workspace_read", "path": "outside.txt"}
            )
        with pytest.raises(PolicyError, match="approval"):
            await broker.request(
                {
                    "capability": "workspace_write",
                    "path": "src/data.txt",
                    "content": "changed",
                }
            )
        with pytest.raises(PolicyError, match="approval"):
            await broker.request({"capability": "credential", "name": "MODEL_KEY"})

    asyncio.run(scenario())


def test_plugin_broker_redacts_delivered_credentials_from_plugin_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async def credential(params):
            return {"name": params["name"], "value": "super-secret"}

        broker = HostCapabilityBroker(
            WorkspacePolicy(tmp_path),
            PluginCapabilities(credentials=("MODEL_KEY",)),
            callbacks=BrokerCallbacks(credential=credential),
        )
        delivered = await broker.request(
            {"capability": "credential", "name": "MODEL_KEY"}
        )
        assert delivered["value"] == "super-secret"
        assert broker.sanitize(
            {"plugin_output": "echo super-secret", "nested": ["super-secret"]}
        ) == {
            "plugin_output": "echo [REDACTED]",
            "nested": ["[REDACTED]"],
        }

    asyncio.run(scenario())


async def _nothing() -> None:
    return None


async def _append(items: list[object], item: object) -> None:
    items.append(item)
