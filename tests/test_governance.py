"""Run governance tests."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from capslock.cli.context import CliContext
from capslock.cli.exec import run_exec
from capslock.configuration import Settings
from capslock.domain import (
    AgentEvent,
    AgentEventKind,
    LoopDetectionSettings,
    RunLimits,
    RunMode,
    RunStopped,
)
from capslock.runtime.governance import RunGovernor
from capslock.runtime import RunRequest
from capslock.runtime.model import ModelMessage, ModelResponse, ModelToolCall
from capslock.storage.repositories import WorkspaceRepositories
from capslock.tooling.contracts import ResolvedToolPolicy, ToolOutcome, define_tool
from capslock.tooling.executor import ToolRuntime
from tests.helpers import FakeChatModel, answer, workflow_service
from tests.test_runtime import make_agent
from rich.console import Console

ToolRegistry = ToolRuntime


def Tool(name, description, schema, execute, **kwargs):
    return define_tool(name, description, schema, execute, **kwargs)


def ToolResult(ok, data, error=None):
    return (
        ToolOutcome.success(data)
        if ok
        else ToolOutcome.failure(error or "failed", data=data)
    )


@pytest.mark.parametrize("version", (1, 2))
def test_non_current_config_is_rejected(
    tmp_path: Path, monkeypatch, version: int
) -> None:
    monkeypatch.setenv("CAPSLOCK_API_KEY", "test-secret")
    config = tmp_path / ".capslock" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        f'config_version = {version}\n[model]\nmodel = "test-model"\n[runtime]\nmax_turns = 9\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported configuration version"):
        Settings.load(tmp_path)


def test_fresh_schema_has_governance_tables(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            version = await repositories.database.fetch_one("PRAGMA user_version")
            assert version[0] == 22
            for table in ("run_governance", "tool_call_attempts"):
                assert await repositories.database.fetch_one(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_exec_stops_before_repeated_tool_side_effect(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")

            async def echo(context, arguments):
                return ToolResult(True, arguments)

            model = FakeChatModel(
                *[
                    ModelResponse(
                        ModelMessage(
                            None, (ModelToolCall(str(index), "echo", '{"x":1}'),)
                        )
                    )
                    for index in range(4)
                ]
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=ToolRegistry([Tool("echo", "echo", {"type": "object"}, echo)]),
            )
            events = [
                event
                async for event in agent.run_stream(
                    RunRequest(
                        question="loop",
                        mode=RunMode.EXEC,
                        limits=RunLimits(max_tool_rounds=10),
                    )
                )
            ]
            assert events[-1].kind is AgentEventKind.STOPPED
            assert events[-1].data["stop_reason"] == "repeated_tool_call"
            attempts = await repositories.database.fetch_one(
                "SELECT count(*) FROM tool_call_attempts WHERE run_id=?",
                (events[-1].run_id,),
            )
            assert attempts[0] == 2
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_budget_snapshot_round_trip(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            prepared = await workflow_service(repositories).prepare(
                session.id, "budget"
            )
            snapshot, _ = await repositories.governance.start(
                prepared.run.id,
                parent_run_id=None,
                mode=RunMode.EXEC,
                limits=RunLimits(max_tool_rounds=3, max_tool_calls=4),
            )
            assert (
                json.loads(json.dumps(snapshot.as_dict()))["limits"]["max_tool_calls"]
                == 4
            )
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_concurrent_tool_attempts_receive_unique_sequences(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            prepared = await workflow_service(repositories).prepare(
                session.id, "parallel reads"
            )
            attempt_ids = await asyncio.gather(
                *(
                    repositories.governance.reserve_attempt(
                        prepared.run.id,
                        round_index=1,
                        name=f"read_{index}",
                        arguments={"index": index},
                        fingerprint=f"fingerprint-{index}",
                    )
                    for index in range(16)
                )
            )
            rows = await repositories.database.fetch_all(
                """SELECT id,sequence FROM tool_call_attempts
                   WHERE run_id=? ORDER BY sequence""",
                (prepared.run.id,),
            )
            assert len(set(attempt_ids)) == 16
            assert [int(row["sequence"]) for row in rows] == list(range(1, 17))
            assert {int(row["id"]) for row in rows} == set(attempt_ids)
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_governor_concurrent_attempts_keep_status_and_limit_atomic(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            prepared = await workflow_service(repositories).prepare(session.id, "tools")
            governor = await RunGovernor.create(
                repositories.governance,
                repositories.models,
                prepared.run.id,
                parent_run_id=None,
                mode=RunMode.EXEC,
                limits=RunLimits(max_tool_calls=2),
                loop_settings=LoopDetectionSettings(),
            )
            first, second = await asyncio.gather(
                governor.before_tool("first", {}),
                governor.before_tool("second", {}),
            )
            await asyncio.gather(
                governor.finish_tool(second[0], ok=False, duration_ms=2),
                governor.finish_tool(first[0], ok=True, duration_ms=1),
            )
            status = {int(item["attempt_id"]): item["ok"] for item in governor.history}
            assert status == {first[0]: True, second[0]: False}
            with pytest.raises(RunStopped):
                await governor.before_tool("third", {})
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_parallel_read_batch_records_attempts_without_sequence_collision(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")

            async def read(context, arguments):
                del context
                await asyncio.sleep(0)
                return ToolResult(True, arguments)

            tools = ToolRegistry(
                [
                    Tool(
                        name,
                        "safe parallel read",
                        {"type": "object"},
                        read,
                        policy=ResolvedToolPolicy.safe_read(),
                    )
                    for name in ("list_files", "git_status", "glob_files")
                ]
            )
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        "Inspecting the workspace.",
                        tuple(
                            ModelToolCall(str(index), name, "{}")
                            for index, name in enumerate(
                                ("list_files", "git_status", "glob_files")
                            )
                        ),
                    )
                ),
                answer("Plan ready."),
            )
            agent = make_agent(tmp_path, repositories, session.id, model, tools=tools)
            events = [
                item
                async for item in agent.run_stream(
                    RunRequest(question="Create a development plan")
                )
            ]
            assert events[-1].kind is AgentEventKind.COMPLETED
            rows = await repositories.database.fetch_all(
                """SELECT sequence,name,ok FROM tool_call_attempts
                   WHERE run_id=? ORDER BY sequence""",
                (events[-1].run_id,),
            )
            assert [int(row["sequence"]) for row in rows] == [1, 2, 3]
            assert {str(row["name"]) for row in rows} == {
                "list_files",
                "git_status",
                "glob_files",
            }
            assert all(int(row["ok"]) == 1 for row in rows)
            governance = await repositories.database.fetch_one(
                "SELECT tool_calls FROM run_governance WHERE run_id=?",
                (events[-1].run_id,),
            )
            assert int(governance["tool_calls"]) == 3
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_exec_stopped_event_returns_exit_code_four() -> None:
    class StoppedAgent:
        async def run_stream(self, request):
            yield AgentEvent(
                1,
                "2026-07-21T00:00:00+00:00",
                "session",
                "run",
                "item",
                AgentEventKind.STOPPED,
                {
                    "status": "stopped",
                    "stop_reason": "max_tool_calls",
                    "budget": {},
                    "error": {
                        "code": "max_tool_calls",
                        "message": "run stopped: max_tool_calls",
                    },
                },
            )

    async def scenario() -> None:
        output = io.StringIO()
        context = CliContext(Console(file=output, force_terminal=False), StoppedAgent())
        assert await run_exec(context, "question", json_events=True) == 4
        assert json.loads(output.getvalue())["data"]["stop_reason"] == "max_tool_calls"

    asyncio.run(scenario())


def test_interactive_soft_limit_can_stop_with_tool_free_summary(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")

            async def echo(context, arguments):
                return ToolResult(True, arguments)

            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(None, (ModelToolCall("first", "echo", "{}"),))
                ),
                answer("work completed so far"),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=ToolRegistry([Tool("echo", "echo", {"type": "object"}, echo)]),
            )

            async def stop(snapshot):
                return False

            events = [
                event
                async for event in agent.run_stream(
                    RunRequest(
                        question="summarize",
                        mode=RunMode.INTERACTIVE,
                        limits=RunLimits(max_tool_rounds=1),
                        authorize_limit=stop,
                    )
                )
            ]
            assert events[-1].kind is AgentEventKind.STOPPED
            assert events[-1].data["answer"] == "work completed so far"
            assert model.requests[-1]["tools"] == []
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_interactive_soft_limit_extension_completes_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")

            async def echo(context, arguments):
                return ToolResult(True, arguments)

            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(None, (ModelToolCall("first", "echo", "{}"),))
                ),
                answer("done"),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=ToolRegistry([Tool("echo", "echo", {"type": "object"}, echo)]),
            )

            async def extend(snapshot):
                return True

            events = [
                event
                async for event in agent.run_stream(
                    RunRequest(
                        question="continue",
                        mode=RunMode.INTERACTIVE,
                        limits=RunLimits(max_tool_rounds=1),
                        authorize_limit=extend,
                    )
                )
            ]
            assert AgentEventKind.BUDGET_EXTENDED in [event.kind for event in events]
            assert events[-1].kind is AgentEventKind.COMPLETED
        finally:
            await repositories.close()

    asyncio.run(scenario())
