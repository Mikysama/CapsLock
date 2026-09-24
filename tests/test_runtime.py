"""Agent runtime tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.application.action_system import (
    ActionCoordinator,
    ActionRunState,
    FileActionHandler,
)
from capslock.domain import (
    ActionStatus,
    ActionType,
    AgentEventKind,
    ApprovalChoice,
    ApprovalDecision,
    ModelErrorCode,
    ModelRoutingError,
    RunStepStatus,
)
from capslock.interaction import RunInteraction
from capslock.observability import EventSink
from capslock.permissions import PermissionMode
from capslock.planning import PlanningService
from capslock.policy import WorkspacePolicy
from capslock.runtime import AgentSession, AsyncOpenAIResponsesModel, RunRequest
from capslock.runtime.model import (
    ModelDelta,
    ModelMessage,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from capslock.runtime.tool_loop import ToolLoop, ToolLoopError
from capslock.storage.repositories import WorkspaceRepositories
from capslock.tooling.contracts import (
    ExecutionContext,
    InterruptBehavior,
    ResolvedToolPolicy,
    ToolOutcome,
    define_tool,
)
from capslock.tooling.executor import ToolRuntime
from capslock.tooling.permission_policy.engine import PermissionEngine
from capslock.tooling.permission_policy.middleware import PermissionMiddleware
from capslock.tooling.planning import PlanningBoundaryMiddleware
from capslock.tooling.tools import workspace_tools
from capslock.tooling.tools.plans import plan_tools
from tests.helpers import (
    DummySkillRegistry,
    DummySkillService,
    FakeChatModel,
    StubActionHandler,
    answer,
    workflow_service,
    workspace_run,
)

ToolRegistry = ToolRuntime


def Tool(name, description, schema, execute, **kwargs):
    return define_tool(name, description, schema, execute, **kwargs)


def ToolResult(ok, data, error=None):
    return (
        ToolOutcome.success(data)
        if ok
        else ToolOutcome.failure(error or "failed", data=data)
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
    permission_engine=None,
    planning=None,
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
        permission_engine=permission_engine,
        planning=planning,
        max_tool_rounds=3,
    )


def test_model_enter_plan_mode_resumes_with_attachment_and_blocks_hidden_tool(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "plan-runtime.sqlite3", workspace=tmp_path
        )
        shell_calls = 0
        try:
            session = await repositories.sessions.create("test-model")
            planning = PlanningService(repositories.plans, root=tmp_path / "plans")

            async def shell(context, arguments):
                nonlocal shell_calls
                shell_calls += 1
                return ToolOutcome.success({"ran": True})

            shell_tool = define_tool(
                "shell",
                "Execute a command.",
                {"type": "object", "properties": {}},
                shell,
                policy=ResolvedToolPolicy(external_side_effects=True),
            )
            engine = PermissionEngine((), repositories.run_journal)
            tools = ToolRuntime(
                [*plan_tools(), shell_tool],
                middleware=(
                    PlanningBoundaryMiddleware(),
                    PermissionMiddleware(engine),
                ),
            )
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (
                            ModelToolCall(
                                "enter-call",
                                "enter_plan_mode",
                                '{"objective":"Design Plan Mode"}',
                            ),
                        ),
                    )
                ),
                answer("Planning is now active."),
                ModelResponse(
                    ModelMessage(
                        None,
                        (ModelToolCall("shell-call", "shell", "{}"),),
                    )
                ),
                answer("The hidden tool was denied."),
                answer("The rejected plan remains available as context."),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=tools,
                permission_engine=engine,
                planning=planning,
            )
            agent.permission_mode = PermissionMode.FULL_ACCESS

            first = await collect(agent, "Plan this change")
            paused = first[-1]
            assert paused.kind is AgentEventKind.WAITING_APPROVAL
            request = await agent.resolve_plan_request(str(paused.data["request_id"]))
            await agent.decide_plan_request(request.id, "enter")

            resumed = [
                event async for event in agent.resume_paused_stream(paused.run_id)
            ]
            assert resumed[-1].kind is AgentEventKind.COMPLETED
            assert any(
                str(message.get("content", "")).startswith("<capslock-plan-mode>")
                for message in model.requests[1]["messages"]
            )
            assert {
                item["function"]["name"] for item in model.requests[1]["tools"]
            } == {"get_plan", "update_plan", "submit_plan"}

            completed = await collect(agent, "Try a forbidden command")
            assert completed[-1].kind is AgentEventKind.COMPLETED
            assert shell_calls == 0
            tool_message = next(
                item
                for item in model.requests[3]["messages"]
                if item.get("role") == "tool"
            )
            assert "plan_mode_read_only" in str(tool_message["content"])

            current = await planning.current(session.id)
            assert current is not None
            plan, revision = current
            request = await repositories.plans.submit(
                plan.id,
                expected_sha256=revision.sha256,
                run_id=None,
                invocation_id=None,
            )
            await repositories.plans.decide(
                request.id,
                choice="reject",
                feedback=None,
                base_permission_mode="full_access",
            )

            after_rejection = await collect(agent, "Continue without Plan Mode")
            assert after_rejection[-1].kind is AgentEventKind.COMPLETED
            historical_plan = next(
                str(message["content"])
                for message in model.requests[4]["messages"]
                if str(message.get("content", "")).startswith("<capslock-plan-context>")
            )
            assert "Plan status: rejected" in historical_plan
            assert '"status":"rejected"' in historical_plan
            assert {
                item["function"]["name"] for item in model.requests[4]["tools"]
            } == {"enter_plan_mode", "shell"}
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_submitted_plan_approval_queues_exactly_one_implementation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "plan-submit.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            planning = PlanningService(repositories.plans, root=tmp_path / "plans")
            plan, revision = await planning.create(
                session.id,
                "Implement the approved change",
                entry_source="slash",
                base_permission_mode="approve_for_me",
                content="# Plan\n\n- Make the approved change.\n- Run tests.\n",
            )
            engine = PermissionEngine((), repositories.run_journal)
            tools = ToolRuntime(
                plan_tools(),
                middleware=(
                    PlanningBoundaryMiddleware(),
                    PermissionMiddleware(engine),
                ),
            )
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (
                            ModelToolCall(
                                "submit-call",
                                "submit_plan",
                                '{"expected_sha256":"' + revision.sha256 + '"}',
                            ),
                        ),
                    )
                ),
                answer("The approved plan is ready for implementation."),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=tools,
                permission_engine=engine,
                planning=planning,
            )

            first = await collect(agent, "Submit the completed plan")
            paused = first[-1]
            request = await agent.resolve_plan_request(str(paused.data["request_id"]))
            await agent.decide_plan_request(request.id, "implement")
            resumed = [
                event async for event in agent.resume_paused_stream(paused.run_id)
            ]
            assert resumed[-1].kind is AgentEventKind.COMPLETED

            implementation = await repositories.plans.implementation(plan.id)
            item = await repositories.work_items.require(implementation.work_item_id)
            assert item.status.value == "queued"
            assert revision.sha256 in item.question
            assert (
                await repositories.plans.require(plan.id)
            ).status.value == "implementing"
            assert (
                len(
                    await repositories.database.fetch_all(
                        "SELECT * FROM plan_implementations WHERE plan_id=?", (plan.id,)
                    )
                )
                == 1
            )
        finally:
            await repositories.close()

    asyncio.run(scenario())


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
                                "create_file",
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


def test_ask_user_resumes_same_run_and_cancels_later_batch_calls(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "pause.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (
                            ModelToolCall(
                                "ask",
                                "ask_user",
                                '{"questions":[{"id":"choice","question":"Choose",'
                                '"options":["A","B"]}]}',
                            ),
                            ModelToolCall(
                                "later",
                                "list_files",
                                '{"path":"."}',
                            ),
                        ),
                    )
                ),
                answer("Continued after the answer."),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=workspace_tools(include_collaboration=False),
            )

            first = await collect(agent, "Ask before continuing")
            paused = first[-1]
            assert paused.kind is AgentEventKind.WAITING_INPUT
            run_id = paused.run_id
            request_id = str(paused.data["request_id"])
            invocations = await repositories.database.fetch_all(
                "SELECT id,name,status FROM tool_invocations WHERE run_id=? ORDER BY sequence",
                (run_id,),
            )
            assert [(row["name"], row["status"]) for row in invocations] == [
                ("ask_user", "waiting_input"),
                ("list_files", "cancelled"),
            ]

            await repositories.run_journal.answer_input_request(
                request_id, session.id, {"choice": "A"}
            )
            resumed = [event async for event in agent.resume_paused_stream(run_id)]
            assert resumed[-1].kind is AgentEventKind.COMPLETED
            assert resumed[-1].run_id == run_id
            resumed_invocation = await repositories.database.fetch_one(
                "SELECT status,execution_status FROM tool_invocations WHERE id=?",
                (str(invocations[0]["id"]),),
            )
            assert resumed_invocation["status"] == "completed"
            assert resumed_invocation["execution_status"] == "succeeded"
        finally:
            await repositories.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "choice,expected_calls,expected_status",
    [
        (ApprovalChoice.APPROVE_ONCE, 1, "succeeded"),
        (ApprovalChoice.REJECT, 0, "denied"),
    ],
)
def test_non_action_permission_request_executes_real_tool_and_resumes(
    tmp_path: Path,
    choice: ApprovalChoice,
    expected_calls: int,
    expected_status: str,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / f"permission-{choice.value}.sqlite3", workspace=tmp_path
        )
        calls = 0
        try:
            session = await repositories.sessions.create("test-model")

            async def inspect(context, arguments):
                nonlocal calls
                calls += 1
                return ToolOutcome.success({"observed": True})

            engine = PermissionEngine((), repositories.run_journal)
            tools = ToolRuntime(
                [define_tool("inspect", "Inspect.", {"type": "object"}, inspect)],
                middleware=(PermissionMiddleware(engine),),
            )
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (ModelToolCall("inspect-call", "inspect", "{}"),),
                    )
                ),
                answer("Continued after permission."),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=tools,
                permission_engine=engine,
            )
            agent.permission_mode = PermissionMode.ASK_FOR_APPROVAL

            first = await collect(agent, "Inspect after approval")
            paused = first[-1]
            assert paused.kind is AgentEventKind.WAITING_APPROVAL
            request = await agent.resolve_permission_request(
                str(paused.data["request_id"])
            )
            decided = await agent.decide_permission_request(str(request["id"]), choice)
            assert decided["result"]["status"] == expected_status
            assert calls == expected_calls

            resumed = [
                event async for event in agent.resume_paused_stream(paused.run_id)
            ]
            assert resumed[-1].kind is AgentEventKind.COMPLETED
            invocation = await repositories.run_journal.tool_invocation(
                str(request["invocation_id"])
            )
            assert invocation is not None and invocation["status"] == (
                "completed" if expected_status == "succeeded" else "failed"
            )
            tool_message = next(
                message
                for message in model.requests[1]["messages"]
                if message.get("role") == "tool"
            )
            assert f'"status": "{expected_status}"' in tool_message["content"]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_non_action_session_approval_persists_before_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "permission-session.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")

            async def inspect(context, arguments):
                return ToolOutcome.success({"observed": True})

            engine = PermissionEngine((), repositories.run_journal)
            tools = ToolRuntime(
                [define_tool("inspect", "Inspect.", {"type": "object"}, inspect)],
                middleware=(PermissionMiddleware(engine),),
            )
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None, (ModelToolCall("inspect-call", "inspect", "{}"),)
                    )
                ),
                answer("Done."),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=tools,
                permission_engine=engine,
            )
            agent.permission_mode = PermissionMode.ASK_FOR_APPROVAL
            paused = (await collect(agent, "Inspect"))[-1]
            request = await agent.resolve_permission_request(
                str(paused.data["request_id"])
            )
            await agent.decide_permission_request(
                str(request["id"]), ApprovalChoice.APPROVE_SESSION
            )
            rules = await repositories.run_journal.session_permission_rules(session.id)
            assert len(rules) == 1
            assert rules[0]["behavior"] == "allow"
            assert rules[0]["tool"] == "inspect"
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


def test_tool_loop_repairs_invalid_arguments_once(tmp_path: Path) -> None:
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
                    ModelMessage(None, (ModelToolCall("bad", "echo", "[invalid]"),))
                ),
                ModelResponse(
                    ModelMessage(None, (ModelToolCall("fixed", "echo", '{"x":1}'),))
                ),
                answer("repaired"),
            )
            loop = ToolLoop(
                chat_model=model,
                model="test",
                tools=ToolRegistry(
                    [
                        Tool(
                            "echo",
                            "echo",
                            {
                                "type": "object",
                                "properties": {"x": {"type": "integer"}},
                                "required": ["x"],
                                "additionalProperties": False,
                            },
                            execute,
                        )
                    ]
                ),
                journal=repositories.run_journal,
                max_tool_rounds=3,
                context_factory=context_factory(repositories, session.id),
            )
            result = await loop.run(
                [], prepared.run.id, emit=lambda kind, data: asyncio.sleep(0)
            )
            assert result.text == "repaired"
            assert [
                item["tools"][0]["function"]["name"] for item in model.requests
            ] == [
                "echo",
                "echo",
                "echo",
            ]
            calls = await repositories.database.fetch_all(
                "SELECT name,ok FROM tool_calls ORDER BY id"
            )
            assert [(row["name"], row["ok"]) for row in calls] == [
                ("echo", 0),
                ("echo", 1),
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_tool_loop_does_not_retry_a_failed_repair(tmp_path: Path) -> None:
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
                    ModelMessage(None, (ModelToolCall("bad", "echo", "[invalid]"),))
                ),
                ModelResponse(
                    ModelMessage(
                        None, (ModelToolCall("still_bad", "echo", "[invalid]"),)
                    )
                ),
                answer("stopped repairing"),
            )
            loop = ToolLoop(
                chat_model=model,
                model="test",
                tools=ToolRegistry([Tool("echo", "echo", {"type": "object"}, execute)]),
                journal=repositories.run_journal,
                max_tool_rounds=3,
                context_factory=context_factory(repositories, session.id),
            )
            result = await loop.run(
                [], prepared.run.id, emit=lambda kind, data: asyncio.sleep(0)
            )
            assert result.text == "stopped repairing"
            calls = await repositories.database.fetch_all(
                "SELECT result_summary FROM tool_calls ORDER BY id"
            )
            assert len(calls) == 2
            assert json.loads(calls[1]["result_summary"])["error_code"] == (
                "argument_repair_exhausted"
            )
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_tool_loop_can_use_two_argument_repairs_when_configured(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "two-repairs.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)

            async def execute(context, arguments):
                return ToolResult(True, arguments)

            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(None, (ModelToolCall("bad-1", "echo", "[invalid]"),))
                ),
                ModelResponse(
                    ModelMessage(None, (ModelToolCall("bad-2", "echo", "[invalid]"),))
                ),
                ModelResponse(
                    ModelMessage(None, (ModelToolCall("fixed", "echo", '{"x":1}'),))
                ),
                answer("repaired twice"),
            )
            loop = ToolLoop(
                chat_model=model,
                model="test",
                tools=ToolRegistry([Tool("echo", "echo", {"type": "object"}, execute)]),
                journal=repositories.run_journal,
                max_tool_rounds=4,
                max_argument_repair_attempts=2,
                context_factory=context_factory(repositories, session.id),
            )
            result = await loop.run(
                [], prepared.run.id, emit=lambda kind, data: asyncio.sleep(0)
            )
            assert result.text == "repaired twice"
            calls = await repositories.database.fetch_all(
                "SELECT result_summary FROM tool_calls ORDER BY id"
            )
            assert len(calls) == 3
            assert json.loads(calls[1]["result_summary"])["data"]["repair_attempt"] == 1
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_async_openai_stream_exposes_reasoning_and_answer_separately() -> None:
    async def scenario() -> None:
        captured = {}
        chunks = [
            SimpleNamespace(
                type="response.reasoning_text.delta",
                delta="inspect the repository",
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                delta="final answer",
            ),
            SimpleNamespace(
                type="response.completed",
                delta=None,
                response=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=3, output_tokens=2)
                ),
            ),
        ]

        class Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not chunks:
                    raise StopAsyncIteration
                return chunks.pop(0)

        class Responses:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return Stream()

        client = SimpleNamespace(responses=Responses())
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": {"type": "object"}},
        }
        deltas = [
            item
            async for item in AsyncOpenAIResponsesModel(client).stream_complete(
                model="test",
                messages=[],
                tools=[],
                response_format=response_format,
            )
        ]
        assert [item.reasoning for item in deltas] == [
            "inspect the repository",
            "",
            "",
        ]
        assert [item.content for item in deltas] == ["", "final answer", ""]
        assert deltas[-1].usage == ModelUsage(3, 2)
        assert captured["text"]["format"] == {
            "type": "json_schema",
            "name": "result",
            "strict": True,
            "schema": {"type": "object"},
        }
        assert captured["input"] == []
        assert captured["stream"] is True

    asyncio.run(scenario())


def test_async_openai_responses_stream_exposes_function_calls() -> None:
    async def scenario() -> None:
        events = [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=1,
                delta=None,
                item=SimpleNamespace(
                    type="function_call",
                    call_id="call-1",
                    id="item-1",
                    name="read",
                    arguments="",
                ),
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                output_index=1,
                delta='{"path":',
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                output_index=1,
                delta='"README.md"}',
            ),
            SimpleNamespace(
                type="response.completed",
                delta=None,
                response=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=8, output_tokens=4)
                ),
            ),
        ]

        class Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not events:
                    raise StopAsyncIteration
                return events.pop(0)

        class Responses:
            async def create(self, **kwargs):
                return Stream()

        deltas = [
            item
            async for item in AsyncOpenAIResponsesModel(
                SimpleNamespace(responses=Responses())
            ).stream_complete(model="test", messages=[], tools=[])
        ]
        assert deltas[0].tool_index == 1
        assert deltas[0].tool_call_id == "call-1"
        assert deltas[0].tool_name == "read"
        assert "".join(item.tool_arguments for item in deltas) == (
            '{"path":"README.md"}'
        )
        assert deltas[-1].usage == ModelUsage(8, 4)

    asyncio.run(scenario())


def test_async_openai_strict_tools_require_nullable_optional_fields() -> None:
    async def scenario() -> None:
        captured = {}

        class Responses:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    output=[],
                    output_text="done",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                )

        client = SimpleNamespace(responses=Responses())
        await AsyncOpenAIResponsesModel(client).complete(
            model="test",
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "old-call",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":"a"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "old-call", "content": "result"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "read",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "limit": {"type": "integer"},
                            },
                            "required": ["path"],
                        },
                    },
                }
            ],
        )
        function = captured["tools"][0]
        assert function["type"] == "function"
        assert function["name"] == "read"
        assert function["strict"] is True
        assert function["parameters"]["additionalProperties"] is False
        assert function["parameters"]["required"] == ["path", "limit"]
        assert function["parameters"]["properties"]["limit"]["type"] == [
            "integer",
            "null",
        ]
        assert captured["input"] == [
            {
                "type": "function_call",
                "call_id": "old-call",
                "name": "read",
                "arguments": '{"path":"a"}',
            },
            {
                "type": "function_call_output",
                "call_id": "old-call",
                "output": "result",
            },
        ]

    asyncio.run(scenario())


def test_async_openai_rejects_non_strict_tool_schema_before_provider_call() -> None:
    async def scenario() -> None:
        called = False

        class Responses:
            async def create(self, **kwargs):
                nonlocal called
                called = True
                raise AssertionError(kwargs)

        client = SimpleNamespace(responses=Responses())
        with pytest.raises(ValueError, match="strict_schema_incompatible"):
            await AsyncOpenAIResponsesModel(client).complete(
                model="test",
                messages=[],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "bad",
                            "description": "bad",
                            "parameters": {"$ref": "#/$defs/input"},
                        },
                    }
                ],
            )
        assert called is False

    asyncio.run(scenario())


def test_async_openai_forwards_provider_response_format() -> None:
    async def scenario() -> None:
        captured = {}

        class Responses:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    output=[],
                    output_text='{"ok":true}',
                    usage=SimpleNamespace(input_tokens=2, output_tokens=1),
                )

        client = SimpleNamespace(responses=Responses())
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": {"type": "object"}},
        }
        result = await AsyncOpenAIResponsesModel(client).complete(
            model="test",
            messages=[],
            tools=[],
            response_format=response_format,
        )

        assert captured["text"]["format"] == {
            "type": "json_schema",
            "name": "result",
            "strict": True,
            "schema": {"type": "object"},
        }
        assert "response_format" not in captured
        assert "messages" not in captured
        assert result.message.content == '{"ok":true}'
        assert result.usage == ModelUsage(2, 1)

    asyncio.run(scenario())


def test_async_openai_maps_incomplete_response_metadata() -> None:
    async def scenario() -> None:
        class Responses:
            async def create(self, **kwargs):
                return SimpleNamespace(
                    output=[],
                    output_text='{"ok":true}',
                    usage=None,
                    status="incomplete",
                    incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                )

        result = await AsyncOpenAIResponsesModel(
            SimpleNamespace(responses=Responses())
        ).complete(model="test", messages=[], tools=[])
        assert result.completion_status == "incomplete"
        assert result.incomplete_reason == "max_output_tokens"

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_recovery", [False, True])
def test_tool_loop_recovers_once_from_pre_output_context_overflow(
    tmp_path: Path,
    cancel_recovery: bool,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "overflow.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            overflow = ModelRoutingError("maximum context length exceeded")
            overflow.code = ModelErrorCode.CONTEXT_OVERFLOW
            model = FakeChatModel(overflow, answer("recovered"))
            events: list[tuple[str, dict[str, object]]] = []

            def factory(run_id: str) -> ExecutionContext:
                return ExecutionContext(
                    session_id=session.id,
                    run_id=run_id,
                    policy=WorkspacePolicy(tmp_path),
                    event=lambda kind, **data: events.append((kind, data)),
                    tasks=repositories.tasks,
                    sources=repositories.sources,
                    actions=None,
                )

            loop = ToolLoop(
                chat_model=model,
                model="test",
                tools=ToolRegistry([]),
                journal=repositories.run_journal,
                max_tool_rounds=1,
                context_factory=factory,
            )
            compact_calls = []

            async def compact(messages, *, force=False):
                compact_calls.append(force)
                if force and cancel_recovery:
                    raise asyncio.CancelledError()
                return [{"role": "user", "content": "compacted"}]

            if cancel_recovery:
                with pytest.raises(asyncio.CancelledError):
                    await loop.run(
                        [{"role": "user", "content": "large"}],
                        prepared.run.id,
                        emit=lambda kind, data: asyncio.sleep(0),
                        compact_context=compact,
                    )
                assert len(model.requests) == 1
                return

            result = await loop.run(
                [{"role": "user", "content": "large"}],
                prepared.run.id,
                emit=lambda kind, data: asyncio.sleep(0),
                compact_context=compact,
            )
            assert result.text == "recovered"
            assert compact_calls == [False, True]
            assert model.requests[1]["messages"][0] == {
                "role": "user",
                "content": "compacted",
            }
            assert [
                kind for kind, _ in events if kind.startswith("context_overflow")
            ] == [
                "context_overflow_detected",
                "context_overflow_recovery_started",
                "context_overflow_recovery_succeeded",
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_tool_loop_stops_after_second_context_overflow(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "overflow-twice.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)

            def overflow():
                error = ModelRoutingError("maximum context length exceeded")
                error.code = ModelErrorCode.CONTEXT_OVERFLOW
                return error

            model = FakeChatModel(overflow(), overflow(), answer("must not run"))
            loop = ToolLoop(
                chat_model=model,
                model="test",
                tools=ToolRegistry([]),
                journal=repositories.run_journal,
                max_tool_rounds=1,
                context_factory=context_factory(repositories, session.id),
            )
            forced = []

            async def compact(messages, *, force=False):
                forced.append(force)
                return messages

            with pytest.raises(ModelRoutingError) as error:
                await loop.run(
                    [{"role": "user", "content": "large"}],
                    prepared.run.id,
                    emit=lambda kind, data: asyncio.sleep(0),
                    compact_context=compact,
                )
            assert error.value.code is ModelErrorCode.CONTEXT_OVERFLOW
            assert forced == [False, True]
            assert len(model.requests) == 2
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_tool_loop_does_not_recover_overflow_after_visible_delta(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "partial-overflow.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)

            class PartialStream:
                calls = 0

                async def stream_complete(self, **request):
                    self.calls += 1
                    yield ModelDelta(content="partial")
                    error = ModelRoutingError("maximum context length exceeded")
                    error.code = ModelErrorCode.CONTEXT_OVERFLOW
                    raise error

            model = PartialStream()
            loop = ToolLoop(
                chat_model=model,
                model="test",
                tools=ToolRegistry([]),
                journal=repositories.run_journal,
                max_tool_rounds=1,
                context_factory=context_factory(repositories, session.id),
            )
            compact_calls = []

            async def compact(messages, *, force=False):
                compact_calls.append(force)
                return messages

            with pytest.raises(ModelRoutingError, match="output started"):
                await loop.run(
                    [],
                    prepared.run.id,
                    emit=lambda kind, data: asyncio.sleep(0),
                    compact_context=compact,
                )
            assert model.calls == 1
            assert compact_calls == [False]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_overflow_recovery_does_not_replay_completed_tool_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "tool-overflow.sqlite3", workspace=tmp_path
        )
        executions = 0
        try:
            session, prepared = await workspace_run(repositories)

            async def execute(context, arguments):
                nonlocal executions
                executions += 1
                return ToolResult(True, {"ok": True})

            overflow = ModelRoutingError("prompt is too long")
            overflow.code = ModelErrorCode.CONTEXT_OVERFLOW
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(None, (ModelToolCall("once", "write_once", "{}"),))
                ),
                overflow,
                answer("recovered"),
            )
            loop = ToolLoop(
                chat_model=model,
                model="test",
                tools=ToolRegistry(
                    [Tool("write_once", "write once", {"type": "object"}, execute)]
                ),
                journal=repositories.run_journal,
                max_tool_rounds=2,
                context_factory=context_factory(repositories, session.id),
            )

            async def compact(messages, *, force=False):
                return messages

            result = await loop.run(
                [],
                prepared.run.id,
                emit=lambda kind, data: asyncio.sleep(0),
                compact_context=compact,
            )
            assert result.text == "recovered"
            assert executions == 1
        finally:
            await repositories.close()

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
            context_events = [
                event
                for event in events
                if event.kind is AgentEventKind.CONTEXT_UPDATED
            ]
            assert [event.data["context"]["source"] for event in context_events] == [
                "estimate",
                "provider",
            ]
            provider_context = context_events[-1].data["context"]
            assert provider_context["used_tokens"] == 11
            assert provider_context["remaining_tokens"] == (
                provider_context["limit_tokens"] - 11
            )
            assert provider_context["used_percent"] == round(
                11 * 100 / provider_context["limit_tokens"], 1
            )
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
            persisted = await repositories.database.fetch_all(
                "SELECT event_kind FROM run_events ORDER BY sequence"
            )
            assert "context_updated" not in {
                str(row["event_kind"]) for row in persisted
            }
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

            tools = ToolRegistry(
                [
                    Tool(
                        "slow",
                        "slow",
                        {"type": "object"},
                        slow,
                        policy=ResolvedToolPolicy(
                            external_side_effects=True,
                            interrupt_behavior=InterruptBehavior.CANCEL,
                        ),
                    )
                ]
            )
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
