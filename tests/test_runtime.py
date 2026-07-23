"""Agent runtime tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.application.action_system import (
    ActionCoordinator,
    ActionRunState,
    FileActionHandler,
)
from capslock.domain import (
    ApprovalDecision,
    ActionStatus,
    ActionType,
    AgentEventKind,
    RunStepStatus,
)
from capslock.observability import EventSink
from capslock.interaction import RunInteraction
from capslock.permissions import PermissionMode
from capslock.policy import WorkspacePolicy
from capslock.runtime import AgentSession, AsyncOpenAIChatModel, RunRequest
from capslock.runtime.model import (
    ModelMessage,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from capslock.runtime.tool_loop import ToolLoop, ToolLoopError
from capslock.storage.repositories import WorkspaceRepositories
from capslock.tooling.async_core import (
    ExecutionContext,
    Tool,
    ToolRegistry,
    ToolResult,
)
from capslock.tooling.async_catalog import workspace_tools
from tests.helpers import (
    DummySkillRegistry,
    DummySkillService,
    FakeChatModel,
    answer,
    workspace_run,
    StubActionHandler,
    workflow_service,
)


def context_factory(repositories: WorkspaceRepositories, session_id: str):
    return lambda run_id: ExecutionContext(
        session_id=session_id,
        run_id=run_id,
        policy=WorkspacePolicy(repositories.sessions.workspace),
        event=lambda *args, **kwargs: None,
        tasks=repositories.tasks,
        sources=repositories.sources,
        actions=None,
    )


async def collect(agent: AgentSession, question: str, **kwargs):
    events = []
    async for event in agent.run_stream(RunRequest(question=question, **kwargs)):
        events.append(event)
    return events


def make_agent(
    tmp_path: Path,
    repositories: WorkspaceRepositories,
    session_id: str,
    model: FakeChatModel,
    *,
    tools: ToolRegistry | None = None,
) -> AgentSession:
    return AgentSession(
        workspace=tmp_path,
        model_name="test-model",
        chat_model=model,
        sessions=repositories.sessions,
        work_items=repositories.work_items,
        runs=repositories.runs,
        journal=repositories.run_journal,
        action_records=repositories.actions,
        tasks=repositories.tasks,
        sources=repositories.sources,
        settings_store=repositories.settings,
        model_audit=repositories.models,
        governance=repositories.governance,
        collaboration_records=repositories.collaboration,
        compactions=repositories.compactions,
        workflow=workflow_service(repositories),
        session_id=session_id,
        policy=WorkspacePolicy(tmp_path),
        action_factory=lambda run_id: None,
        skill_registry=DummySkillRegistry(),
        skill_service=DummySkillService(),
        events=EventSink(),
        tools=tools or ToolRegistry([]),
        permission_mode=PermissionMode.APPROVE_FOR_ME,
        max_tool_rounds=3,
    )


def test_agent_model_switch_is_session_scoped_and_blocked_during_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "model.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("deepseek-v4-flash")
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                FakeChatModel(answer("unused")),
            )
            selected = await agent.set_model("deepseek-v4-pro")
            assert selected == "deepseek-v4-pro"
            assert agent.model == "deepseek-v4-pro"
            assert agent.tool_loop.model == "deepseek-v4-pro"
            assert agent.tool_loop.model_steps.model == "deepseek-v4-pro"
            assert (await repositories.sessions.require(session.id)).model == selected

            agent._active_runs = 1
            with pytest.raises(ValueError, match="run is active"):
                await agent.set_model("deepseek-v4-flash")
            assert agent.model == "deepseek-v4-pro"
            with pytest.raises(ValueError, match="deepseek-v4-flash"):
                await agent.set_model("unsupported")
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_interactive_approval_executes_action_inside_same_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (
                            ModelToolCall(
                                "create",
                                "propose_file_create",
                                '{"path":"approved.txt","content":"done\\n"}',
                            ),
                        ),
                    )
                ),
                answer("The approved file was created."),
            )
            interaction = RunInteraction(permission_mode=PermissionMode.APPROVE_FOR_ME)

            def actions(run_id: str) -> ActionCoordinator:
                return ActionCoordinator(
                    repositories.actions,
                    ActionRunState(repositories.runs, repositories.workflow),
                    session_id=session.id,
                    run_id=run_id,
                    handlers=[
                        FileActionHandler(WorkspacePolicy(tmp_path)),
                        StubActionHandler(
                            set(ActionType)
                            - {ActionType.FILE_CREATE, ActionType.FILE_EDIT}
                        ),
                    ],
                    event=lambda *args, **kwargs: None,
                    interaction=interaction,
                )

            agent = AgentSession(
                workspace=tmp_path,
                model_name="test-model",
                chat_model=model,
                sessions=repositories.sessions,
                work_items=repositories.work_items,
                runs=repositories.runs,
                journal=repositories.run_journal,
                action_records=repositories.actions,
                tasks=repositories.tasks,
                sources=repositories.sources,
                settings_store=repositories.settings,
                model_audit=repositories.models,
                governance=repositories.governance,
                collaboration_records=repositories.collaboration,
                compactions=repositories.compactions,
                workflow=workflow_service(repositories),
                session_id=session.id,
                policy=WorkspacePolicy(tmp_path),
                action_factory=actions,
                skill_registry=DummySkillRegistry(),
                skill_service=DummySkillService(),
                events=EventSink(),
                tools=workspace_tools(),
                permission_mode=PermissionMode.APPROVE_FOR_ME,
                max_tool_rounds=3,
                interaction=interaction,
            )
            decisions = []

            async def approve(action):
                decisions.append(action.id)
                return ApprovalDecision.APPROVE

            agent.set_action_authorizer(approve)
            events = await collect(agent, "Create approved.txt")
            assert len(decisions) == 1
            assert (tmp_path / "approved.txt").read_text() == "done\n"
            assert events[-1].kind is AgentEventKind.COMPLETED
            assert all(
                event.kind is not AgentEventKind.WAITING_APPROVAL for event in events
            )
            actions_for_run = await repositories.actions.list(
                session.id, run_id=events[-1].run_id
            )
            assert [item.status for item in actions_for_run] == [ActionStatus.COMPLETED]
            tool_message = next(
                message
                for message in model.requests[1]["messages"]
                if message.get("role") == "tool"
            )
            assert '"status": "completed"' in tool_message["content"]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_tool_loop_handles_invalid_arguments_and_continues(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)

            async def execute(context, arguments):
                return ToolResult(True, arguments)

            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(None, (ModelToolCall("call_1", "echo", "[invalid]"),))
                ),
                answer("recovered", input_tokens=3, output_tokens=2),
            )
            loop = ToolLoop(
                chat_model=model,
                model="test",
                tools=ToolRegistry([Tool("echo", "echo", {"type": "object"}, execute)]),
                journal=repositories.run_journal,
                max_tool_rounds=2,
                context_factory=context_factory(repositories, session.id),
            )
            emitted = []

            async def emit(kind, data):
                emitted.append(kind)

            result = await loop.run([], prepared.run.id, emit=emit)
            assert result.text == "recovered"
            assert (result.input_tokens, result.output_tokens) == (3, 2)
            calls = await repositories.database.fetch_all("SELECT * FROM tool_calls")
            assert len(calls) == 1 and calls[0]["ok"] == 0
            assert AgentEventKind.TOOL_COMPLETED in emitted
            steps = await repositories.database.fetch_all(
                "SELECT status FROM run_steps ORDER BY ordinal"
            )
            assert [row[0] for row in steps] == ["completed", "failed", "completed"]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_async_openai_stream_exposes_reasoning_and_answer_separately() -> None:
    async def scenario() -> None:
        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            reasoning_content="inspect the repository",
                            content=None,
                            tool_calls=(),
                        )
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            reasoning_content=None,
                            content="final answer",
                            tool_calls=(),
                        )
                    )
                ],
                usage=None,
            ),
        ]

        class Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not chunks:
                    raise StopAsyncIteration
                return chunks.pop(0)

        class Completions:
            async def create(self, **kwargs):
                return Stream()

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        deltas = [
            item
            async for item in AsyncOpenAIChatModel(client).stream_complete(
                model="test", messages=[], tools=[]
            )
        ]
        assert [item.reasoning for item in deltas] == [
            "inspect the repository",
            "",
        ]
        assert [item.content for item in deltas] == ["", "final answer"]

    asyncio.run(scenario())


def test_tool_loop_emits_reasoning_as_thinking_delta(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            loop = ToolLoop(
                chat_model=FakeChatModel(
                    ModelResponse(ModelMessage("answer", reasoning="step by step"))
                ),
                model="test",
                tools=ToolRegistry([]),
                journal=repositories.run_journal,
                max_tool_rounds=1,
                context_factory=context_factory(repositories, session.id),
            )
            emitted = []

            async def emit(kind, data):
                emitted.append((kind, data))

            result = await loop.run([], prepared.run.id, emit=emit)
            assert result.text == "answer"
            assert emitted[:3] == [
                (AgentEventKind.THINKING, {}),
                (AgentEventKind.THINKING, {"text": "step by step"}),
                (AgentEventKind.TEXT_DELTA, {"text": "answer"}),
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (ModelResponse(ModelMessage(None)), "empty answer"),
        (
            ModelResponse(
                ModelMessage(None, (ModelToolCall("call", "unknown", "{}"),))
            ),
            "maximum number",
        ),
    ],
)
def test_tool_loop_records_failed_model_steps(
    tmp_path: Path, response: ModelResponse, message: str
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / message.replace(" ", "-") / "state.sqlite3",
            workspace=tmp_path,
        )
        try:
            session, prepared = await workspace_run(repositories)
            loop = ToolLoop(
                chat_model=FakeChatModel(response),
                model="test",
                tools=ToolRegistry([]),
                journal=repositories.run_journal,
                max_tool_rounds=0,
                context_factory=context_factory(repositories, session.id),
            )

            async def emit(kind, data):
                return None

            with pytest.raises(ToolLoopError, match=message):
                await loop.run([], prepared.run.id, emit=emit)
            step = await repositories.database.fetch_one(
                "SELECT status,error FROM run_steps"
            )
            assert step["status"] == RunStepStatus.FAILED.value
            assert step["error"]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_agent_stream_has_one_terminal_event_and_persists_usage(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                FakeChatModel(answer("final answer", input_tokens=11, output_tokens=5)),
            )
            events = await collect(agent, "question")
            terminal = [event for event in events if event.terminal]
            assert len(terminal) == 1
            assert terminal[0].kind is AgentEventKind.COMPLETED
            assert terminal[0].data["answer"] == "final answer"
            assert terminal[0].data["usage"]["input_tokens"] == 11
            messages = await repositories.sessions.messages(session.id)
            assert messages == [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "final answer"},
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_reopened_agent_receives_resumed_session_context(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            first_model = FakeChatModel(answer("first answer"))
            await collect(
                make_agent(tmp_path, repositories, session.id, first_model),
                "first question",
            )

            resumed_model = FakeChatModel(answer("second answer"))
            await collect(
                make_agent(tmp_path, repositories, session.id, resumed_model),
                "follow-up question",
            )
            messages = resumed_model.requests[0]["messages"]
            assert messages[-4:-1] == [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "follow-up question"},
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_interrupted_question_is_available_after_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            failing = make_agent(
                tmp_path,
                repositories,
                session.id,
                FakeChatModel(ModelResponse(ModelMessage(None))),
            )
            with pytest.raises(Exception, match="empty answer"):
                await collect(failing, "interrupted question")

            resumed_model = FakeChatModel(answer("recovered"))
            await collect(
                make_agent(tmp_path, repositories, session.id, resumed_model),
                "continue",
            )
            assert {
                "role": "user",
                "content": "interrupted question",
            } in resumed_model.requests[0]["messages"]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_agent_failure_has_one_terminal_event_and_no_running_step(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                FakeChatModel(ModelResponse(ModelMessage(None))),
            )
            events = []
            with pytest.raises(Exception, match="empty answer"):
                async for event in agent.run_stream(RunRequest(question="question")):
                    events.append(event)
            assert [event.kind for event in events if event.terminal] == [
                AgentEventKind.FAILED
            ]
            assert (
                await repositories.database.fetch_one(
                    "SELECT count(*) FROM run_steps WHERE status='running'"
                )
            )[0] == 0
            assert (
                await repositories.database.fetch_one(
                    "SELECT count(*) FROM run_events WHERE event_kind IN ('completed','failed','cancelled','waiting_approval')"
                )
            )[0] == 1
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_checkpoint_resume_uses_last_stable_async_step(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")

            async def echo(context, arguments):
                return ToolResult(True, {"echo": arguments["value"]})

            tools = ToolRegistry(
                [
                    Tool(
                        "echo",
                        "echo",
                        {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                        echo,
                    )
                ]
            )
            first_model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (ModelToolCall("call", "echo", '{"value":"one"}'),),
                    ),
                    ModelUsage(2, 1),
                ),
                RuntimeError("transport failed"),
            )
            first = make_agent(
                tmp_path, repositories, session.id, first_model, tools=tools
            )
            first_events = []
            with pytest.raises(RuntimeError, match="transport failed"):
                async for event in first.run_stream(RunRequest(question="question")):
                    first_events.append(event)
            failed = next(
                event for event in first_events if event.kind is AgentEventKind.FAILED
            )
            checkpoint = await repositories.run_journal.last_stable_step(failed.run_id)
            assert checkpoint is not None

            second_model = FakeChatModel(answer("resumed"))
            second = make_agent(
                tmp_path, repositories, session.id, second_model, tools=tools
            )
            resumed = await collect(
                second, "question", resume_from_run_id=failed.run_id
            )
            completed = next(
                event for event in resumed if event.kind is AgentEventKind.COMPLETED
            )
            run = await repositories.runs.require(completed.run_id)
            assert run.parent_run_id == failed.run_id
            assert run.resume_from_step_id == checkpoint.id
            assert any(
                message.get("role") == "tool"
                for message in second_model.requests[0]["messages"]
            )
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_cancelling_stream_closes_run_action_and_running_step(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            started = asyncio.Event()
            action_id = None

            async def slow(context, arguments):
                nonlocal action_id
                action = await repositories.actions.create(
                    session_id=session.id,
                    run_id=context.run_id,
                    action_type=ActionType.COMMAND,
                    summary="running",
                    request={"argv": []},
                )
                await repositories.actions.transition(action.id, ActionStatus.APPROVED)
                await repositories.actions.transition(action.id, ActionStatus.RUNNING)
                action_id = action.id
                started.set()
                await asyncio.Event().wait()
                return ToolResult(True, {})

            tools = ToolRegistry([Tool("slow", "slow", {"type": "object"}, slow)])
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                FakeChatModel(
                    ModelResponse(
                        ModelMessage(None, (ModelToolCall("call", "slow", "{}"),))
                    )
                ),
                tools=tools,
            )

            async def consume() -> None:
                async for _ in agent.run_stream(RunRequest(question="cancel me")):
                    pass

            task = asyncio.create_task(consume())
            # Full-suite CI may have several aiosqlite workers draining when this
            # scenario starts; the assertion is about cancellation cleanup, not
            # sub-two-second startup latency.
            await asyncio.wait_for(started.wait(), timeout=30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert action_id is not None
            action = await repositories.actions.require(action_id)
            assert action.status is ActionStatus.CANCELLED
            run = await repositories.runs.require(action.run_id)
            item = await repositories.work_items.require(run.work_item_id)
            assert run.status == item.status.value == "cancelled"
            assert (
                await repositories.database.fetch_one(
                    "SELECT count(*) FROM run_steps WHERE run_id=? AND status='running'",
                    (run.id,),
                )
            )[0] == 0
            events = await repositories.run_journal.events(run.id)
            assert [entry.kind for entry in events if entry.terminal] == [
                AgentEventKind.CANCELLED
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())
