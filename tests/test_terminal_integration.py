"""Failed model output remains readable without executing incomplete calls."""

import asyncio

import pytest

from capslock.domain import AgentEventKind
from capslock.runtime.engine import RunRequest
from capslock.runtime.model import (
    ModelDelta,
    ModelResponse,
    ModelMessage,
    ModelToolCall,
)
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import FakeChatModel
from tests.test_runtime import make_agent, ToolRegistry, Tool, ToolResult


@pytest.mark.parametrize("terminal", ["incomplete", "failed", None, "duplicate"])
def test_unsuccessful_stream_keeps_partial_transcript_without_tool_execution(
    tmp_path, terminal
):
    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / "state.db", workspace=tmp_path
        )
        executions = []

        async def effect(context, arguments):
            executions.append(arguments)
            return ToolResult(True, {})

        class StreamingModel:
            async def stream_complete(self, **kwargs):
                yield ModelDelta(reasoning="partial reasoning")
                yield ModelDelta(content="partial answer")
                yield ModelDelta(
                    tool_index=0,
                    tool_call_id="call",
                    tool_name="effect",
                    tool_arguments="{}",
                )
                if terminal == "duplicate":
                    yield ModelDelta(completion_status="completed")
                    yield ModelDelta(completion_status="completed")
                elif terminal:
                    yield ModelDelta(completion_status=terminal)

        try:
            session = await repo.sessions.create("test-model")
            tool = Tool(
                "effect", "test effect", {"type": "object", "properties": {}}, effect
            )
            agent = make_agent(
                tmp_path, repo, session.id, StreamingModel(), tools=ToolRegistry([tool])
            )
            events = []
            with pytest.raises(Exception):
                async for event in agent.run_stream(RunRequest(question="do it")):
                    events.append(event)
            assert executions == []
            assert [e.kind for e in events if e.terminal] == [AgentEventKind.FAILED]
            transcript = await repo.sessions.transcript(session.id)
            assert transcript[-1]["content"] == "partial answer"
            assert transcript[-1]["reasoning_content"] == "partial reasoning"
            row = await repo.database.fetch_one(
                "SELECT checkpoint_json FROM run_steps WHERE kind='model'"
            )
            assert "partial reasoning" in row[0]
            assert "tool_calls" not in row[0]
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_nonstream_explicit_failure_never_executes_tools(tmp_path):
    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / "state.db", workspace=tmp_path
        )
        executions = []

        async def effect(context, arguments):
            executions.append(arguments)
            return ToolResult(True, {})

        try:
            session = await repo.sessions.create("test-model")
            response = ModelResponse(
                ModelMessage(
                    "partial", (ModelToolCall("c", "effect", "{}"),), "reason"
                ),
                completion_status="failed",
            )
            agent = make_agent(
                tmp_path,
                repo,
                session.id,
                FakeChatModel(response),
                tools=ToolRegistry(
                    [
                        Tool(
                            "effect",
                            "effect",
                            {"type": "object", "properties": {}},
                            effect,
                        )
                    ]
                ),
            )
            events = []
            with pytest.raises(Exception):
                async for event in agent.run_stream(RunRequest(question="do it")):
                    events.append(event)
            assert executions == []
            assert [e.kind for e in events if e.terminal] == [AgentEventKind.FAILED]
            assert (await repo.sessions.transcript(session.id))[-1][
                "content"
            ] == "partial"
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_cancellation_preserves_partial_checkpoint(tmp_path):
    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / "cancel.db", workspace=tmp_path
        )
        ready = asyncio.Event()

        class StreamingModel:
            async def stream_complete(self, **kwargs):
                yield ModelDelta(content="before cancel", reasoning="thinking")
                ready.set()
                await asyncio.Event().wait()

        try:
            session = await repo.sessions.create("test-model")
            agent = make_agent(tmp_path, repo, session.id, StreamingModel())

            async def consume():
                async for _ in agent.run_stream(RunRequest(question="wait")):
                    pass

            task = asyncio.create_task(consume())
            await ready.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            row = await repo.database.fetch_one(
                "SELECT checkpoint_json FROM run_steps WHERE kind='model'"
            )
            assert "before cancel" in row[0]
            assert "thinking" in row[0]
            run = await repo.database.fetch_one("SELECT status FROM runs")
            assert run[0] == "cancelled"
        finally:
            await repo.close()

    asyncio.run(scenario())
