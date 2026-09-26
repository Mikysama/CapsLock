"""Local JSON-RPC protocol exercises with real AgentSession and SQLite."""

import asyncio
from types import SimpleNamespace

import pytest

from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import FakeChatModel, answer
from tests.test_runtime import make_agent


def test_app_server_handshake_idempotency_events_and_restart(tmp_path):
    from capslock.app_server import AppServer

    async def scenario():
        calls = []
        notifications = []

        async def factory(session_id=None):
            repositories = await WorkspaceRepositories.open(
                tmp_path / "state.sqlite3", workspace=tmp_path
            )
            session = (
                await repositories.sessions.get(session_id)
                if session_id
                else await repositories.sessions.create("test-model")
            )
            model = FakeChatModel(answer("done"))
            calls.append(model)
            return SimpleNamespace(
                session=make_agent(tmp_path, repositories, session.id, model),
                repositories=repositories,
                close=repositories.close,
            )

        async def emit(message):
            notifications.append(message)

        server = AppServer(factory, emit)

        async def rpc(method, **params):
            return await server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )

        assert (await rpc("session/create"))["error"]["code"] == -32001
        assert (await rpc("initialize", protocol_version=1))["result"][
            "protocol_version"
        ] == 1
        session_id = (await rpc("session/create"))["result"]["session_id"]
        await rpc("events/subscribe", session_id=session_id)
        first = (
            await rpc(
                "run/start", session_id=session_id, request_id="once", question="hello"
            )
        )["result"]
        duplicate = (
            await rpc(
                "run/start", session_id=session_id, request_id="once", question="hello"
            )
        )["result"]
        assert first["work_item_id"] == duplicate["work_item_id"]
        await server.wait_idle()
        assert len(calls[0].requests) == 1
        complete = [
            item["params"]
            for item in notifications
            if item["method"] == "run/event" and item["params"]["terminal"]
        ]
        assert len(complete) == 1 and complete[0]["event"] == "completed"
        assert complete[0]["status"] == "completed"
        run_id = complete[0]["run_id"]
        assert (
            await rpc(
                "run/start",
                session_id=session_id,
                request_id="once",
                question="different",
            )
        )["error"]["code"] == -32602
        await server.close()
        server = AppServer(factory, emit)
        await rpc("initialize", protocol_version=1)
        await rpc("session/resume", session_id=session_id)
        replay = (
            await rpc(
                "run/start", session_id=session_id, request_id="once", question="hello"
            )
        )["result"]
        assert replay["run_id"] == run_id
        assert not calls[1].requests
        history = (await rpc("events/subscribe", session_id=session_id, run_id=run_id))[
            "result"
        ]
        assert history["events"][-1]["terminal"]
        page = (
            await rpc("events/subscribe", session_id=session_id, run_id=run_id, limit=1)
        )["result"]
        assert len(page["events"]) == 1 and page["has_more"]
        second = (
            await rpc(
                "events/subscribe",
                session_id=session_id,
                run_id=run_id,
                limit=1,
                after_sequence=page["next_sequence"],
            )
        )["result"]
        assert second["events"][0]["sequence"] > page["events"][0]["sequence"]
        await server.close()

    asyncio.run(scenario())


def test_stdio_rejects_oversized_message_and_closes(monkeypatch):
    import io
    import json
    from capslock.app_server import serve_stdio

    stream = io.StringIO("x" * (1024 * 1024 + 1) + "\n")
    output = io.StringIO()
    monkeypatch.setattr("sys.stdin", stream)
    monkeypatch.setattr("sys.stdout", output)
    assert asyncio.run(serve_stdio(None, None, None)) == 0
    assert json.loads(output.getvalue())["error"]["message"] == "message exceeds 1 MiB"


@pytest.mark.parametrize("decision", ["enter", "implement"])
def test_app_server_plan_decision_resumes_same_run(tmp_path, decision):
    from capslock.app_server import AppServer
    from capslock.planning import PlanningService
    from capslock.runtime.model import ModelMessage, ModelResponse, ModelToolCall
    from capslock.tooling.executor import ToolRuntime
    from capslock.tooling.tools.plans import plan_tools

    async def scenario():
        messages = []

        async def factory(session_id=None):
            repositories = await WorkspaceRepositories.open(
                tmp_path / "plan.sqlite3", workspace=tmp_path
            )
            session = await repositories.sessions.create("test-model")
            planning = PlanningService(repositories.plans, root=tmp_path / "plans")
            call = ModelToolCall("plan", "enter_plan_mode", '{"objective":"Plan work"}')
            if decision == "implement":
                _, revision = await planning.create(
                    session.id,
                    "Implement work",
                    entry_source="slash",
                    base_permission_mode="approve_for_me",
                    content="# Plan\n\nImplement work and test.\n",
                )
                call = ModelToolCall(
                    "plan",
                    "submit_plan",
                    '{"expected_sha256":"' + revision.sha256 + '"}',
                )
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (call,),
                    )
                ),
                answer("planning"),
                answer("implemented"),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=ToolRuntime(plan_tools()),
                planning=planning,
            )
            return SimpleNamespace(
                session=agent, repositories=repositories, close=repositories.close
            )

        async def emit(message):
            messages.append(message)

        server = AppServer(factory, emit)

        async def rpc(method, **params):
            return await server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )

        try:
            await rpc("initialize", protocol_version=1)
            sid = (await rpc("session/create"))["result"]["session_id"]
            await rpc("events/subscribe", session_id=sid)
            await rpc(
                "run/start", session_id=sid, request_id="plan", question="plan work"
            )
            await server.wait_idle()
            paused = messages[-1]["params"]
            assert paused["event"] == "waiting_approval"
            response = await rpc(
                "approval/answer",
                session_id=sid,
                request_id=paused["data"]["request_id"],
                choice=decision,
            )
            assert response["result"]["run_id"] == paused["run_id"]
            await server.wait_idle()
            assert messages[-1]["params"]["event"] == "completed"
            if decision == "enter":
                assert await server.application.session.current_plan() is not None
            else:
                rows = await server.application.repositories.database.fetch_all(
                    "SELECT * FROM plan_implementations"
                )
                assert len(rows) == 1
                item = await server.application.repositories.work_items.require(
                    rows[0]["work_item_id"]
                )
                assert str(item.status) == "completed"
        finally:
            await server.close()

    asyncio.run(scenario())


def test_app_server_input_answer_resumes_same_run(tmp_path):
    from capslock.app_server import AppServer
    from capslock.runtime.model import ModelMessage, ModelResponse, ModelToolCall
    from capslock.tooling.tools import workspace_tools

    async def scenario():
        notifications = []

        async def factory(session_id=None):
            repositories = await WorkspaceRepositories.open(
                tmp_path / "state.sqlite3", workspace=tmp_path
            )
            session = await repositories.sessions.create("test-model")
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (
                            ModelToolCall(
                                "ask",
                                "ask_user",
                                '{"questions":[{"id":"choice","question":"Choose","options":["A","B"]}]}',
                            ),
                        ),
                    )
                ),
                answer("resumed"),
            )
            return SimpleNamespace(
                session=make_agent(
                    tmp_path,
                    repositories,
                    session.id,
                    model,
                    tools=workspace_tools(include_collaboration=False),
                ),
                repositories=repositories,
                close=repositories.close,
            )

        async def emit(message):
            notifications.append(message)

        server = AppServer(factory, emit)

        async def rpc(method, **params):
            return await server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )

        await rpc("initialize", protocol_version=1)
        sid = (await rpc("session/create"))["result"]["session_id"]
        await rpc("events/subscribe", session_id=sid)
        await rpc("run/start", session_id=sid, request_id="ask", question="ask")
        await server.wait_idle()
        paused = notifications[-1]["params"]
        assert paused["event"] == "waiting_input"
        response = await rpc(
            "input/answer",
            session_id=sid,
            request_id=paused["data"]["request_id"],
            answers={"choice": "A"},
        )
        assert response["result"]["run_id"] == paused["run_id"]
        await server.wait_idle()
        assert notifications[-1]["params"]["event"] == "completed"
        assert notifications[-1]["params"]["run_id"] == paused["run_id"]
        await server.close()

    asyncio.run(scenario())


def test_app_server_approval_answers_authorizer_and_disconnect_cancels(tmp_path):
    from capslock.app_server import AppServer
    from capslock.domain import ActionType, ApprovalChoice

    async def scenario():
        notified = asyncio.Event()

        async def emit(message):
            notified.set()

        application = SimpleNamespace(session=SimpleNamespace(session_id="session"))
        server = AppServer(None, emit)
        server.initialized = True
        server.application = application
        task = asyncio.create_task(
            server._authorize(
                SimpleNamespace(
                    id="action",
                    session_id="session",
                    run_id="run",
                    summary="Create file",
                    type=ActionType.FILE_CREATE,
                )
            )
        )
        await notified.wait()
        response = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "approval/answer",
                "params": {
                    "session_id": "session",
                    "request_id": "action",
                    "choice": "approve_once",
                },
            }
        )
        assert response["result"]["status"] == "answered"
        assert await task is ApprovalChoice.APPROVE_ONCE
        assert not server.approvals

    asyncio.run(scenario())


def test_stdio_outputs_protocol_only_and_handles_parse_error(monkeypatch):
    import io
    import json
    from capslock.app_server import serve_stdio

    stream = io.StringIO(
        'bad json\n{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocol_version":1}}\n'
    )
    output = io.StringIO()
    monkeypatch.setattr("sys.stdin", stream)
    monkeypatch.setattr("sys.stdout", output)
    assert asyncio.run(serve_stdio(None, None, None)) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[0]["error"]["code"] == -32700
    assert messages[1]["result"]["protocol_version"] == 1


def test_app_server_event_pipe_failure_closes_application(tmp_path):
    from capslock.app_server import AppServer

    async def scenario():
        closed = asyncio.Event()

        async def factory(session_id=None):
            repositories = await WorkspaceRepositories.open(
                tmp_path / "broken.sqlite3", workspace=tmp_path
            )
            session = await repositories.sessions.create("test-model")

            async def close():
                await repositories.close()
                closed.set()

            return SimpleNamespace(
                session=make_agent(
                    tmp_path, repositories, session.id, FakeChatModel(answer("done"))
                ),
                repositories=repositories,
                close=close,
            )

        async def emit(message):
            raise BrokenPipeError("client output pipe closed")

        server = AppServer(factory, emit)

        async def rpc(method, **params):
            return await server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )

        await rpc("initialize", protocol_version=1)
        sid = (await rpc("session/create"))["result"]["session_id"]
        await rpc("events/subscribe", session_id=sid)
        await rpc("run/start", session_id=sid, request_id="pipe", question="hello")
        try:
            await asyncio.wait_for(closed.wait(), 2)
            assert server.closed
        finally:
            await server.close()

    asyncio.run(scenario())


def test_app_server_action_approval_executes_once_and_uses_permission_kernel(tmp_path):
    from capslock.app_server import AppServer
    from capslock.application.action_system import (
        ActionCoordinator,
        ActionRunState,
        FileActionHandler,
    )
    from capslock.policy import WorkspacePolicy
    from capslock.domain import ActionType
    from tests.helpers import StubActionHandler
    from capslock.runtime.model import ModelMessage, ModelResponse, ModelToolCall
    from capslock.tooling.tools import workspace_tools

    async def scenario():
        messages = []
        pending = asyncio.Event()

        async def factory(session_id=None):
            repositories = await WorkspaceRepositories.open(
                tmp_path / "approval.sqlite3", workspace=tmp_path
            )
            session = await repositories.sessions.create("test-model")
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (
                            ModelToolCall(
                                "write",
                                "create_file",
                                '{"path":"approved.txt","content":"approved"}',
                            ),
                        ),
                    )
                ),
                answer("done"),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=workspace_tools(include_collaboration=False),
            )
            agent.action_factory = lambda run_id: ActionCoordinator(
                repositories.actions,
                ActionRunState(repositories.runs, repositories.workflow),
                session_id=session.id,
                run_id=run_id,
                handlers=[
                    FileActionHandler(WorkspacePolicy(tmp_path)),
                    StubActionHandler(
                        set(ActionType)
                        - {
                            ActionType.FILE_CREATE,
                            ActionType.FILE_EDIT,
                            ActionType.NOTEBOOK_EDIT,
                        }
                    ),
                ],
                event=lambda *args, **kwargs: None,
                interaction=agent.interaction,
            )
            return SimpleNamespace(
                session=agent, repositories=repositories, close=repositories.close
            )

        async def emit(message):
            messages.append(message)
            if message.get("method") == "approval/request":
                pending.set()

        server = AppServer(factory, emit)

        async def rpc(method, **params):
            return await server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )

        await rpc("initialize", protocol_version=1)
        sid = (await rpc("session/create"))["result"]["session_id"]
        await rpc("events/subscribe", session_id=sid)
        await rpc(
            "run/start",
            session_id=sid,
            request_id="write",
            question="create approved.txt",
        )
        await asyncio.wait_for(pending.wait(), 5)
        assert not (tmp_path / "approved.txt").exists()
        identifier = next(
            message["params"]["request_id"]
            for message in messages
            if message["method"] == "approval/request"
        )
        assert "error" in await rpc(
            "approval/answer",
            session_id="wrong",
            request_id=identifier,
            choice="approve_once",
        )
        assert "result" in await rpc(
            "approval/answer",
            session_id=sid,
            request_id=identifier,
            choice="approve_once",
        )
        await server.wait_idle()
        assert (tmp_path / "approved.txt").read_text() == "approved"
        assert messages[-1]["params"]["event"] == "completed"
        repeated = await rpc(
            "run/start",
            session_id=sid,
            request_id="write",
            question="create approved.txt",
        )
        assert repeated["result"]["duplicate"]
        actions = await server.application.repositories.database.fetch_all(
            "SELECT status FROM actions"
        )
        assert len(actions) == 1 and actions[0][0] == "completed"
        await server.close()

    asyncio.run(scenario())


def test_app_server_eof_cancels_active_and_rejects_invalid_requests(tmp_path):
    from capslock.app_server import AppServer

    async def scenario():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        class SlowModel:
            async def complete(self, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        async def factory(session_id=None):
            repositories = await WorkspaceRepositories.open(
                tmp_path / "state.sqlite3", workspace=tmp_path
            )
            session = await repositories.sessions.create("test-model")
            return SimpleNamespace(
                session=make_agent(tmp_path, repositories, session.id, SlowModel()),
                repositories=repositories,
                close=repositories.close,
            )

        async def emit(message):
            pass

        server = AppServer(factory, emit)
        assert (await server.handle([]))["error"]["code"] == -32600
        invalid = await server.handle(
            {"jsonrpc": "2.0", "id": [], "method": "initialize"}
        )
        assert invalid["id"] is None
        assert (
            await server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocol_version": 2},
                }
            )
        )["error"]["code"] == -32602

        async def rpc(method, **params):
            return await server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )

        await rpc("initialize", protocol_version=1)
        sid = (await rpc("session/create"))["result"]["session_id"]
        await rpc("run/start", session_id=sid, request_id="slow", question="wait")
        await entered.wait()
        assert "error" in await rpc("session/create")
        await server.close()
        assert cancelled.is_set()
        repos = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        rows = await repos.database.fetch_all("SELECT status FROM runs")
        assert rows and all(row[0] != "running" for row in rows)
        await repos.close()

    asyncio.run(scenario())
