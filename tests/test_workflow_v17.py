from __future__ import annotations

import asyncio
import json
from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from capslock.cli.context import CliContext
from capslock.cli import app as cli
from capslock.cli import actions as cli_actions
from capslock.cli.exec import APPROVAL_REQUIRED_EXIT, run_exec
from capslock.cli.prompt import prompt_footer
from capslock.cli.render import render_session_history, render_workflow_status
from capslock.cli.tui import _queue_command, _run_request
from capslock.cli.prompt import prompt_session
from capslock.domain import AgentEvent, AgentEventKind, MemoryOrigin, MemoryScope, MemoryType, RunStepStatus, WorkItemStatus
from capslock.model import ModelDelta, ModelMessage, ModelResponse, ModelUsage
from capslock.model import ModelToolCall
from capslock.runtime import AgentRuntimeError, WorkspaceAgent
from capslock.runtime_support import CancellationToken
from capslock.session import SessionStore
from capslock.session_management import SessionManager
from capslock.storage import MemoryStore, workspace_key
from capslock.storage.migrations import SCHEMA_VERSION
from capslock.theme import make_console
from capslock.tooling.core import Tool, ToolRegistry, ToolResult


class StreamingModel:
    async def stream_complete(self, **kwargs):
        yield ModelDelta(content="hello ")
        yield ModelDelta(content="world")
        yield ModelDelta(usage=ModelUsage(4, 2))


class CompleteModel:
    def __init__(self, answer: str = "done") -> None:
        self.answer = answer

    def complete(self, **kwargs) -> ModelResponse:
        return ModelResponse(ModelMessage(self.answer), ModelUsage(2, 1))


class SequenceModel:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses

    def complete(self, **kwargs) -> ModelResponse:
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, ModelResponse)
        return response


def test_schema_v6_work_items_steps_events_and_reorder(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "state.sqlite3") as store:
        session = store.create(tmp_path, "test")
        first = store.enqueue_work_item(session.id, "first")
        second = store.enqueue_work_item(session.id, "second")
        assert store.reorder_work_item(second.id, 0).position == 0
        run_id = store.start_run(session.id, "second", work_item_id=second.id)
        step = store.create_run_step(run_id, "model")
        stable = store.finish_run_step(
            step.id,
            status=RunStepStatus.COMPLETED,
            checkpoint={"messages": [{"role": "user", "content": "second"}]},
        )
        event = store.append_run_event(
            session_id=session.id,
            run_id=run_id,
            work_item_id=second.id,
            kind=AgentEventKind.THINKING,
            payload={},
        )
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
        assert store.last_stable_step(run_id) == stable
        assert store.run_events(run_id) == [event]
        assert store.get_work_item(first.id).status is WorkItemStatus.QUEUED


def test_streaming_agent_persists_ordered_events_and_usage(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(StreamingModel(), workspace=tmp_path, model="test", store=store)
    item = agent.enqueue("say hello")

    async def collect():
        return [event async for event in agent.ask_stream("say hello", work_item_id=item.id)]

    events = asyncio.run(collect())
    assert [event.kind for event in events] == [
        AgentEventKind.QUEUED,
        AgentEventKind.THINKING,
        AgentEventKind.TEXT_DELTA,
        AgentEventKind.TEXT_DELTA,
        AgentEventKind.COMPLETED,
    ]
    assert agent.last_answer is not None and agent.last_answer.text == "hello world"
    assert store.session_cost(agent.session_id)[:2] == (4, 2)
    assert [item.sequence for item in events] == list(range(1, 6))
    assert len(store.list_work_items(agent.session_id)) == 1


def test_cancellation_marks_run_work_item_and_pending_actions(tmp_path: Path) -> None:
    class SlowModel:
        async def stream_complete(self, **kwargs):
            while True:
                await asyncio.sleep(0)
                yield ModelDelta(content="x")

    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(SlowModel(), workspace=tmp_path, model="test", store=store)
    token = CancellationToken()

    async def cancel_after_delta():
        output = []
        with pytest.raises(asyncio.CancelledError):
            async for event in agent.ask_stream("work", cancellation=token):
                output.append(event)
                if event.kind is AgentEventKind.TEXT_DELTA:
                    token.cancel()
        return output

    events = asyncio.run(cancel_after_delta())
    assert events[-1].kind is AgentEventKind.CANCELLED
    item = store.list_work_items(agent.session_id)[0]
    assert item.status is WorkItemStatus.CANCELLED


def test_session_search_archive_export_and_two_phase_delete(tmp_path: Path) -> None:
    memory_path = tmp_path / "user-memory.sqlite3"
    with SessionStore(tmp_path / "state.sqlite3") as store, MemoryStore(memory_path) as memory:
        session = store.create(tmp_path, "test")
        store.append_message(session.id, "user", "中文 roadmap secret=visible")
        memory.create(
            content="temporary session fact",
            memory_type=MemoryType.FACT,
            scope=MemoryScope.SESSION,
            workspace=workspace_key(tmp_path),
            session_id=session.id,
            source_kind="manual",
            source_ref=None,
            confidence=1,
            expires_at=None,
            origin=MemoryOrigin.MANUAL,
        )
        retained = memory.create(
            content="workspace fact from this session",
            memory_type=MemoryType.FACT,
            scope=MemoryScope.WORKSPACE,
            workspace=workspace_key(tmp_path),
            session_id=session.id,
            source_kind="automatic",
            source_ref="run",
            confidence=1,
            expires_at=None,
            origin=MemoryOrigin.AUTOMATIC,
        )
        assert store.search_sessions("roadmap")[0].id == session.id
        store.archive_session(session.id)
        assert not store.search_sessions("roadmap")
        assert store.search_sessions("roadmap", include_archived=True)[0].archived_at is not None
        manager = SessionManager(store, workspace=tmp_path, memory_store=memory)
        output = manager.export(session.id, "exports/demo")
        document = json.loads((output / "session.json").read_text(encoding="utf-8"))
        assert document["format"] == "capslock-session-export"
        assert "secret=<redacted>" in json.dumps(document)
        assert (output / "session.md").is_file()
        assert manager.delete(session.id) == 1
        assert store.get(session.id) is None
        assert memory.get(retained.id, include_inactive=True).source_valid is False


def test_exec_text_and_jsonl_contract(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(CompleteModel("final answer"), workspace=tmp_path, model="test", store=store)
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)
    assert run_exec(CliContext(console, agent), "question") == 0
    assert stream.getvalue().strip() == "final answer"

    second_store = SessionStore(tmp_path / "other.sqlite3")
    second = WorkspaceAgent(CompleteModel(), workspace=tmp_path, model="test", store=second_store)
    events = StringIO()
    assert run_exec(CliContext(Console(file=events, force_terminal=False), second), "question", json_events=True) == 0
    records = [json.loads(line) for line in events.getvalue().splitlines()]
    assert records[0]["schema_version"] == 1
    assert records[-1]["event"] == "completed"


def test_exec_returns_approval_exit_and_persists_risk(tmp_path: Path) -> None:
    model = SequenceModel([
        ModelResponse(ModelMessage(None, (ModelToolCall("call", "propose_file_create", json.dumps({"path": "note.txt", "content": "hello"})),))),
        ModelResponse(ModelMessage("Approval is required.")),
    ])
    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(model, workspace=tmp_path, model="test", store=store)
    output = StringIO()
    code = run_exec(CliContext(Console(file=output, force_terminal=False), agent), "create note")
    assert code == APPROVAL_REQUIRED_EXIT
    action = store.list_actions(agent.session_id)[0]
    assert action.status.value == "pending"
    assert action.risk_level == "high" and action.risk_reason and action.rollback
    cli_actions.action_coordinator(agent, action.run_id).approve_and_execute(action.id)
    cli_actions.settle_workflow(agent, action.run_id)
    assert store.list_work_items(agent.session_id)[0].status is WorkItemStatus.COMPLETED
    assert store.run_events(action.run_id)[-1].data == {"approval_settled": True}


def test_tui_queue_can_reorder_and_cancel_before_start(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(CompleteModel(), workspace=tmp_path, model="test", store=store)
    first = agent.enqueue("first")
    second = agent.enqueue("second")
    pending = [(first.id, first.question, None), (second.id, second.question, None)]
    context = CliContext(make_console(file=StringIO(), force_terminal=False), agent)

    _queue_command(context, f"/queue move {second.id[:8]} 1", pending)
    assert pending[0][0] == second.id
    _queue_command(context, f"/queue cancel {second.id[:8]}", pending)
    assert store.get_work_item(second.id).status is WorkItemStatus.CANCELLED
    assert [item[0] for item in pending] == [first.id]


def test_session_management_cli_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CAPSLOCK_MEMORY_DATABASE", str(tmp_path / "memory.sqlite3"))
    with SessionStore(tmp_path / ".capslock" / "state" / "capslock.sqlite3") as store:
        session = store.create(tmp_path, "test")
        store.append_message(session.id, "user", "searchable release note")
    output = StringIO()
    console = make_console(file=output, force_terminal=False)

    assert cli.main(["--workspace", str(tmp_path), "sessions", "search", "searchable"], console=console) == 0
    assert cli.main(["--workspace", str(tmp_path), "sessions", "archive", session.id[:8]], console=console) == 0
    assert cli.main(["--workspace", str(tmp_path), "sessions", "unarchive", session.id[:8]], console=console) == 0
    assert cli.main(["--workspace", str(tmp_path), "sessions", "export", session.id[:8], "session-export"], console=console) == 0
    assert (tmp_path / "session-export" / "session.json").is_file()
    assert cli.main(["--workspace", str(tmp_path), "sessions", "delete", session.id[:8], "--yes"], console=console) == 0


def test_retry_resumes_last_tool_checkpoint_without_replay(tmp_path: Path) -> None:
    calls = 0

    def execute(context, arguments):
        nonlocal calls
        calls += 1
        return ToolResult(True, {"value": "stable"})

    tools = ToolRegistry([Tool("checkpoint_tool", "test", {"type": "object", "properties": {}}, execute)])
    model = SequenceModel([
        ModelResponse(ModelMessage(None, (ModelToolCall("call", "checkpoint_tool", "{}"),))),
        RuntimeError("transport failed"),
    ])
    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(model, workspace=tmp_path, model="test", store=store, tools=tools)
    with pytest.raises(RuntimeError, match="transport failed"):
        agent.ask("run tool")
    failed_run = store._connection.execute("SELECT id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()[0]
    assert calls == 1 and store.last_stable_step(failed_run) is not None
    model.responses.append(ModelResponse(ModelMessage("resumed")))

    async def resume():
        return [event async for event in agent.ask_stream("run tool", resume_from_run_id=failed_run)]

    events = asyncio.run(resume())
    assert events[-1].kind is AgentEventKind.COMPLETED
    assert agent.last_answer is not None and agent.last_answer.text == "resumed"
    assert calls == 1


@pytest.mark.parametrize("max_turns", [1, 32])
def test_max_turns_allows_one_final_synthesis_without_an_extra_tool(tmp_path: Path, max_turns: int) -> None:
    calls = 0

    def execute(context, arguments):
        nonlocal calls
        calls += 1
        return ToolResult(True, {"round": calls})

    tools = ToolRegistry([Tool("inspect", "test", {"type": "object", "properties": {}}, execute)])
    tool_call = ModelResponse(ModelMessage(None, (ModelToolCall("call", "inspect", "{}"),)))
    model = SequenceModel([tool_call] * max_turns + [ModelResponse(ModelMessage("summary"))])
    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(model, workspace=tmp_path, model="test", store=store, tools=tools, max_turns=max_turns)

    assert agent.ask("inspect then summarize").text == "summary"
    assert calls == max_turns


def test_synthesis_turn_rejects_another_tool_call_without_executing_it(tmp_path: Path) -> None:
    calls = 0

    def execute(context, arguments):
        nonlocal calls
        calls += 1
        return ToolResult(True, {})

    tools = ToolRegistry([Tool("inspect", "test", {"type": "object", "properties": {}}, execute)])
    tool_call = ModelResponse(ModelMessage(None, (ModelToolCall("call", "inspect", "{}"),)))
    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(
        SequenceModel([tool_call, tool_call, tool_call]),
        workspace=tmp_path,
        model="test",
        store=store,
        tools=tools,
        max_turns=2,
    )

    with pytest.raises(Exception, match="maximum number of tool-call rounds"):
        agent.ask("keep inspecting")
    assert calls == 2


def test_tui_renders_a_failed_event_only_once(tmp_path: Path) -> None:
    class FailingAgent:
        last_answer = None

        async def ask_stream(self, *args, **kwargs):
            yield AgentEvent(
                1,
                "now",
                "session",
                "run",
                "work",
                AgentEventKind.FAILED,
                {"error": "boom"},
            )
            raise AgentRuntimeError("boom")

    output = StringIO()
    context = CliContext(make_console(file=output, force_terminal=False), FailingAgent())
    asyncio.run(
        _run_request(
            context,
            "question",
            False,
            work_item_id="work",
            token=CancellationToken(),
        )
    )

    assert output.getvalue().count("Error: boom") == 1


def test_resumed_tui_renders_complete_and_interrupted_session_context(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    session = store.create(tmp_path, "test")
    store.append_message(session.id, "user", "completed question", run_id="completed-run")
    store.append_message(session.id, "assistant", "completed answer", run_id="completed-run")
    item = store.enqueue_work_item(session.id, "interrupted question")
    run_id = store.start_run(session.id, item.question, work_item_id=item.id)
    store.append_run_event(
        session_id=session.id,
        run_id=run_id,
        work_item_id=item.id,
        kind=AgentEventKind.TEXT_DELTA,
        payload={"text": "partial answer"},
    )
    store.finish_run(run_id, status="cancelled", duration_ms=1, error="cancelled by user")
    agent = WorkspaceAgent(CompleteModel(), workspace=tmp_path, model="test", store=store, session_id=session.id)
    output = StringIO()

    render_session_history(make_console(file=output, force_terminal=False), agent)

    rendered = output.getvalue()
    assert "Resumed:" in rendered and session.id[:12] in rendered
    assert "completed question" in rendered and "completed answer" in rendered
    assert "interrupted question" in rendered and "partial answer" in rendered
    assert "ended as cancelled" in rendered


def test_tui_flushes_streamed_answer_and_survives_transport_errors() -> None:
    class StreamingAgent:
        last_answer = None

        async def ask_stream(self, *args, **kwargs):
            yield AgentEvent(1, "now", "session", "run", "work", AgentEventKind.THINKING, {})
            yield AgentEvent(2, "now", "session", "run", "work", AgentEventKind.TEXT_DELTA, {"text": "hello "})
            yield AgentEvent(3, "now", "session", "run", "work", AgentEventKind.TEXT_DELTA, {"text": "world"})

    class TransportErrorAgent:
        last_answer = None

        async def ask_stream(self, *args, **kwargs):
            yield AgentEvent(1, "now", "session", "run", "work", AgentEventKind.THINKING, {})
            raise RuntimeError("connection dropped")

    output = StringIO()
    console = make_console(file=output, force_terminal=False)
    activities = []
    asyncio.run(
        _run_request(
            CliContext(console, StreamingAgent()),
            "question",
            False,
            work_item_id="work",
            token=CancellationToken(),
            set_activity=activities.append,
        )
    )
    asyncio.run(
        _run_request(
            CliContext(console, TransportErrorAgent()),
            "question",
            False,
            work_item_id="work",
            token=CancellationToken(),
        )
    )

    rendered = output.getvalue()
    assert "⠋ Thinking..." in rendered
    assert "hello world" in rendered
    assert "Model or transport error: connection dropped" in rendered
    assert "thinking" in activities and activities[-1] is None


def test_thinking_footer_uses_distinct_spinner_frames() -> None:
    first = "".join(text for _, text in prompt_footer(activity="thinking", spinner_frame=0))
    second = "".join(text for _, text in prompt_footer(activity="thinking", spinner_frame=1))

    assert "⠋ Thinking..." in first
    assert "⠙ Thinking..." in second


def test_tui_stream_output_is_preserved_while_prompt_is_active() -> None:
    class StreamingAgent:
        last_answer = None

        async def ask_stream(self, *args, **kwargs):
            yield AgentEvent(1, "now", "session", "run", "work", AgentEventKind.THINKING, {})
            yield AgentEvent(2, "now", "session", "run", "work", AgentEventKind.TEXT_DELTA, {"text": "visible\noutput"})

    output = StringIO()
    context = CliContext(make_console(file=output, force_terminal=False), StreamingAgent())

    async def exercise(pipe) -> None:
        inputs = prompt_session()

        async def stream() -> None:
            await asyncio.sleep(0.01)
            await _run_request(
                context,
                "question",
                False,
                work_item_id="work",
                token=CancellationToken(),
            )
            pipe.send_text("done\r")

        worker = asyncio.create_task(stream())
        assert await inputs.prompt_async("> ") == "done"
        await worker

    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            asyncio.run(exercise(pipe))

    assert "visible\noutput" in output.getvalue()


@pytest.mark.parametrize("width", [80, 120, 160])
def test_workflow_status_is_readable_at_supported_widths(tmp_path: Path, width: int) -> None:
    store = SessionStore(tmp_path / f"state-{width}.sqlite3")
    agent = WorkspaceAgent(CompleteModel(), workspace=tmp_path, model="test", store=store)
    store.replace_tasks(agent.session_id, ["检查窄终端布局"], run_id="run")
    store.enqueue_work_item(agent.session_id, "render status")
    output = StringIO()
    render_workflow_status(make_console(file=output, width=width, force_terminal=False), agent, width=width)
    rendered = output.getvalue()
    assert "Plan" in rendered and "Queue" in rendered and "Usage" in rendered
