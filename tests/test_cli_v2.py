from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from capslock.cli.app import async_main, build_parser
from capslock.cli.commands import COMMANDS, command_completions, resolve_command
from capslock.cli.context import CliContext
from capslock.cli.diagnostics import delete_session
from capslock.cli.diagnostics import select_session as select_saved_session
from capslock.cli.dispatch import dispatch_slash_command
from capslock.cli.exec import run_exec
from capslock.cli.prompt import (
    prompt_footer,
    select_action_decision,
    select_permission_mode,
    select_session,
)
from capslock.cli.tui import (
    _RunRenderer,
    _TerminalWriter,
    _authorize_action,
    _set_activity,
)
from capslock.cli.views.common import CAPSLOCK_ART, startup
from capslock.cli.views.workflow import StatusView, render_status, result_status
from capslock.domain import (
    AgentEvent,
    AgentEventKind,
    ApprovalDecision,
    ActionRecord,
    ActionStatus,
    ActionType,
    SessionInfo,
    SessionTitleSource,
    WorkItemInfo,
    WorkItemStatus,
)
from capslock.permissions import PermissionMode
from capslock.theme import make_console


class EventAgent:
    def __init__(self, events: list[AgentEvent]) -> None:
        self.events = events

    async def ask_stream(self, question: str):
        for event in self.events:
            yield event


class TerminalThenErrorAgent(EventAgent):
    async def ask_stream(self, question: str):
        for event in self.events:
            yield event
        raise RuntimeError("already represented by terminal event")


def event(
    kind: AgentEventKind, data: dict[str, object], sequence: int = 1
) -> AgentEvent:
    return AgentEvent(
        sequence,
        "2026-01-01T00:00:00+00:00",
        "session",
        "run",
        "work",
        kind,
        data,
    )


def console_buffer(*, width: int = 120) -> tuple[Console, io.StringIO]:
    output = io.StringIO()
    return (
        make_console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=width,
        ),
        output,
    )


def test_parser_exposes_only_v2_top_level_commands() -> None:
    parser = build_parser()
    choices = next(
        action.choices for action in parser._actions if getattr(action, "choices", None)
    )
    assert set(choices) == {
        "exec",
        "resume",
        "session",
        "sessions",
        "doctor",
        "init",
        "config",
        "credentials",
        "backup",
        "export",
        "import",
    }
    delete = parser.parse_args(["session", "delete"])
    assert delete.command == "session"
    assert delete.sessions_command == "delete"
    assert delete.session_id is None
    for removed in ("chat", "ask", "migrate-layout"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed])


def test_slash_command_catalog_has_no_v1_aliases() -> None:
    expected = {
        "/help",
        "/status",
        "/permissions",
        "/approvals",
        "/queue",
        "/memory",
        "/skills",
        "/sources",
        "/mcp",
        "/diff",
        "/undo",
        "/rename",
        "/exit",
        "/quit",
    }
    assert {item.path for item in COMMANDS} == expected
    assert resolve_command("/status") is not None
    assert resolve_command("/quit") is not None
    assert command_completions("/s") == ["/status", "/skills", "/sources"]
    for removed in ("/cost", "/context", "/tasks", "/changes", "/commands", "/web"):
        assert resolve_command(removed) is None


@pytest.mark.parametrize(
    ("kind", "data", "expected"),
    [
        (
            AgentEventKind.COMPLETED,
            {
                "status": "completed",
                "answer": "done",
                "citations": [],
                "memory_recalls": [],
                "usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": 0},
                "duration_ms": 4,
            },
            0,
        ),
        (
            AgentEventKind.WAITING_APPROVAL,
            {"status": "waiting_approval", "action_ids": ["a"]},
            3,
        ),
        (
            AgentEventKind.FAILED,
            {"status": "failed", "error": {"code": "model_error", "message": "bad"}},
            1,
        ),
        (
            AgentEventKind.CANCELLED,
            {"status": "cancelled", "error": {"code": "cancelled", "message": "stop"}},
            130,
        ),
    ],
)
def test_jsonl_v2_terminal_contract_and_exit_codes(
    kind: AgentEventKind, data: dict[str, object], expected: int
) -> None:
    async def scenario() -> None:
        console, output = console_buffer()
        context = CliContext(console, EventAgent([event(kind, data)]))
        assert await run_exec(context, "question", json_events=True) == expected
        record = json.loads(output.getvalue())
        assert list(record) == [
            "schema_version",
            "sequence",
            "timestamp",
            "session_id",
            "work_item_id",
            "run_id",
            "event",
            "status",
            "terminal",
            "data",
        ]
        assert record["schema_version"] == 2
        assert record["event"] == kind.value
        assert record["terminal"] is True
        assert record["status"] == data["status"]
        assert record["data"] == data

    asyncio.run(scenario())


def test_jsonl_stream_sequences_nonterminal_and_terminal_events() -> None:
    async def scenario() -> None:
        console, output = console_buffer()
        events = [
            event(AgentEventKind.THINKING, {}, 1),
            event(AgentEventKind.TEXT_DELTA, {"text": "ok"}, 2),
            event(
                AgentEventKind.COMPLETED,
                {
                    "status": "completed",
                    "answer": "ok",
                    "citations": [],
                    "memory_recalls": [],
                    "usage": {},
                    "duration_ms": 1,
                },
                3,
            ),
        ]
        assert (
            await run_exec(
                CliContext(console, EventAgent(events)), "question", json_events=True
            )
            == 0
        )
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        assert [item["sequence"] for item in records] == [1, 2, 3]
        assert [item["terminal"] for item in records] == [False, False, True]
        assert [item["status"] for item in records] == [
            "running",
            "running",
            "completed",
        ]

    asyncio.run(scenario())


def test_jsonl_does_not_append_plain_text_after_failed_terminal() -> None:
    async def scenario() -> None:
        console, output = console_buffer()
        failed = event(
            AgentEventKind.FAILED,
            {
                "status": "failed",
                "error": {"code": "transport_error", "message": "failed"},
            },
        )
        context = CliContext(console, TerminalThenErrorAgent([failed]))
        assert await run_exec(context, "question", json_events=True) == 1
        lines = output.getvalue().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["event"] == "failed"

    asyncio.run(scenario())


def test_default_entry_rejects_non_tty_and_points_to_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("CAPSLOCK_MEMORY_DATABASE", str(tmp_path / "memory.sqlite3"))
        monkeypatch.setattr(
            "capslock.cli.app.sys.stdin", SimpleNamespace(isatty=lambda: False)
        )
        console, output = console_buffer()
        assert await async_main(["--workspace", str(tmp_path)], console=console) == 2
        assert "requires a terminal" in output.getvalue()
        assert "capslock exec" in output.getvalue()

    asyncio.run(scenario())


@pytest.mark.parametrize("width", [80, 120, 160])
def test_typed_status_view_renders_at_supported_widths(
    tmp_path: Path, width: int
) -> None:
    console, output = console_buffer(width=width)
    session = SessionInfo(
        "s" * 32,
        tmp_path,
        "model",
        "2026-01-01",
        "2026-01-01",
        "A compact session title",
        SessionTitleSource.MANUAL,
    )
    work = WorkItemInfo(
        "w" * 32,
        session.id,
        "Inspect the repository and report actionable findings",
        WorkItemStatus.WAITING_APPROVAL,
        0,
        "2026-01-01",
        "2026-01-01",
    )
    render_status(
        console,
        StatusView(
            session,
            str(tmp_path),
            "test-model",
            PermissionMode.APPROVE_FOR_ME.value,
            [],
            [work],
            10,
            4,
            0.01,
            2,
            24,
        ),
    )
    rendered = output.getvalue()
    assert "A compact session title" in rendered
    assert "waiting_approval" in rendered
    assert "Inspect the repository" in rendered


@pytest.mark.parametrize("width", [80, 120, 160])
def test_character_art_banner_renders_at_supported_widths(
    tmp_path: Path, width: int
) -> None:
    console, output = console_buffer(width=width)
    startup(
        console,
        workspace=str(tmp_path),
        model="test-model",
        session_id="a" * 32,
        permission_mode=PermissionMode.APPROVE_FOR_ME,
    )
    rendered = output.getvalue()
    assert CAPSLOCK_ART[0] in rendered
    assert "CapsLock v" in rendered
    assert "test-model" in rendered
    assert "approve_for_me" in rendered
    assert "Welcome back!" in rendered
    if width >= 110:
        assert "Tips for getting started" in rendered


def test_session_selector_uses_arrow_keys_and_enter(tmp_path: Path) -> None:
    sessions = [
        SessionInfo(
            "a" * 32,
            tmp_path,
            "model",
            "2026-07-15T10:00:00+00:00",
            "2026-07-15T10:00:00+00:00",
            "First session",
            SessionTitleSource.MANUAL,
        ),
        SessionInfo(
            "b" * 32,
            tmp_path,
            "model",
            "2026-07-16T11:30:00+00:00",
            "2026-07-16T11:30:00+00:00",
            "Second session",
            SessionTitleSource.MANUAL,
        ),
    ]
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            selected = select_session(sessions, width=120)
    assert selected == "b" * 32


def test_permission_selector_uses_current_default_and_arrow_keys() -> None:
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            selected = select_permission_mode(PermissionMode.APPROVE_FOR_ME)
    assert selected is PermissionMode.ASK_FOR_APPROVAL


def test_action_selector_uses_arrow_keys_and_enter() -> None:
    action = ActionRecord(
        "action-id",
        "session",
        "run",
        ActionType.FILE_EDIT,
        ActionStatus.PENDING,
        "Update README",
        {"path": "README.md", "diff": "change"},
        None,
        None,
        "2026-07-21T00:00:00+00:00",
        risk_level="high",
    )
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            selected = select_action_decision(action)
    assert selected == "approve"


def test_action_selector_defaults_to_reject() -> None:
    action = ActionRecord(
        "action-id",
        "session",
        "run",
        ActionType.FILE_EDIT,
        ActionStatus.PENDING,
        "Update README",
        {"path": "README.md"},
        None,
        None,
        "2026-07-21T00:00:00+00:00",
        risk_level="high",
    )
    with create_pipe_input() as pipe:
        pipe.send_text("\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            selected = select_action_decision(action)
    assert selected is ApprovalDecision.REJECT


def test_bare_permissions_opens_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    selected: list[str] = []

    class Settings:
        async def set_workspace(self, key: str, value: str) -> None:
            selected.extend((key, value))

    agent = SimpleNamespace(
        permission_mode=PermissionMode.APPROVE_FOR_ME,
        repositories=SimpleNamespace(settings=Settings()),
    )
    monkeypatch.setattr(
        "capslock.cli.actions.select_permission_mode",
        lambda current: PermissionMode.ASK_FOR_APPROVAL,
    )
    console, output = console_buffer()
    asyncio.run(dispatch_slash_command(CliContext(console, agent), "/permissions"))
    assert agent.permission_mode is PermissionMode.ASK_FOR_APPROVAL
    assert selected == ["permission_mode", "ask_for_approval"]
    assert "Permission mode: ask_for_approval" in output.getvalue()


def test_inline_action_authorizer_returns_selected_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = ActionRecord(
        "action-id",
        "session",
        "run",
        ActionType.COMMAND,
        ActionStatus.PENDING,
        "Run tests",
        {"template": "pytest"},
        None,
        None,
        "2026-07-21T00:00:00+00:00",
        risk_level="high",
    )

    async def run_selector(function, **kwargs):
        assert kwargs == {"in_executor": True}
        return function()

    monkeypatch.setattr("capslock.cli.tui.run_in_terminal", run_selector)
    monkeypatch.setattr(
        "capslock.cli.tui.select_action_decision",
        lambda proposal: ApprovalDecision.APPROVE,
    )
    console, _ = console_buffer()
    context = CliContext(console, SimpleNamespace())
    decision = asyncio.run(_authorize_action(context, action))
    assert decision is ApprovalDecision.APPROVE


def test_inline_action_authorizer_runs_choice_outside_active_event_loop() -> None:
    action = ActionRecord(
        "action-id",
        "session",
        "run",
        ActionType.FILE_CREATE,
        ActionStatus.PENDING,
        "Create a large file whose payload must stay hidden",
        {"path": "secret.py", "after_content": "sensitive payload"},
        None,
        None,
        "2026-07-21T00:00:00+00:00",
        risk_level="high",
    )
    console, output = console_buffer()
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            decision = asyncio.run(
                _authorize_action(CliContext(console, SimpleNamespace()), action)
            )
    assert decision is ApprovalDecision.APPROVE
    assert "sensitive payload" not in output.getvalue()
    assert "secret.py" not in output.getvalue()


@pytest.mark.parametrize("error_type", [EOFError, KeyboardInterrupt])
def test_inline_action_authorizer_cancellation_rejects(
    monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    action = ActionRecord(
        "action-id",
        "session",
        "run",
        ActionType.COMMAND,
        ActionStatus.PENDING,
        "Run tests",
        {"template": "pytest"},
        None,
        None,
        "2026-07-21T00:00:00+00:00",
    )

    async def cancelled(function, **kwargs):
        raise error_type()

    monkeypatch.setattr("capslock.cli.tui.run_in_terminal", cancelled)
    console, _ = console_buffer()
    decision = asyncio.run(
        _authorize_action(CliContext(console, SimpleNamespace()), action)
    )
    assert decision is ApprovalDecision.REJECT


def test_resume_entry_uses_interactive_session_selector(tmp_path: Path) -> None:
    sessions = [
        SessionInfo(
            char * 32,
            tmp_path,
            "model",
            f"2026-07-{day}T10:00:00+00:00",
            f"2026-07-{day}T10:00:00+00:00",
            title,
            SessionTitleSource.MANUAL,
        )
        for char, day, title in (
            ("a", "15", "First session"),
            ("b", "16", "Second session"),
        )
    ]

    class Sessions:
        async def list(self, limit: int):
            assert limit == 20
            return sessions

    repositories = SimpleNamespace(sessions=Sessions())
    console, _ = console_buffer()
    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            selected = asyncio.run(select_saved_session(console, repositories, 20))
    assert selected == "b" * 32


def test_interactive_delete_returns_to_selector_after_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = [
        SessionInfo(
            char * 32,
            tmp_path,
            "model",
            f"2026-07-{day}T10:00:00+00:00",
            f"2026-07-{day}T10:00:00+00:00",
            title,
            SessionTitleSource.MANUAL,
        )
        for char, day, title in (
            ("a", "15", "Keep this session"),
            ("b", "16", "Delete this session"),
        )
    ]

    class Sessions:
        async def list(self, limit: int):
            assert limit == 20
            return sessions

        async def resolve(self, session_id: str):
            return next((item for item in sessions if item.id == session_id), None)

    class Manager:
        deleted: list[str] = []

        async def delete(self, session_id: str) -> int:
            self.deleted.append(session_id)
            return 2

    class ConsoleStub:
        width = 120
        answers = ["n", "y"]
        prompts: list[str] = []
        messages: list[str] = []

        def input(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return self.answers.pop(0)

        def print(self, message: str) -> None:
            self.messages.append(message)

    selected = [sessions[0].id, sessions[1].id]
    selector_titles: list[str] = []

    def choose_session(items, width, *, title):
        assert items == sessions
        assert width == 120
        selector_titles.append(title)
        return selected.pop(0)

    monkeypatch.setattr("capslock.cli.diagnostics.choose_session", choose_session)
    manager = Manager()
    console = ConsoleStub()
    result = asyncio.run(
        delete_session(
            console,
            manager,
            SimpleNamespace(sessions=Sessions()),
            None,
        )
    )

    assert result == 0
    assert selector_titles == ["Delete a session", "Delete a session"]
    assert manager.deleted == [sessions[1].id]
    assert 'session "Keep this session"' in console.prompts[0]
    assert 'session "Delete this session"' in console.prompts[1]
    assert "purged 2 session memories" in console.messages[0]


def test_quit_alias_exits_tui() -> None:
    console, _ = console_buffer()
    context = CliContext(console, SimpleNamespace())
    assert asyncio.run(dispatch_slash_command(context, "/quit")) == "exit"


def test_activity_footer_animates_thinking_and_running() -> None:
    thinking_0 = "".join(
        item[1] for item in prompt_footer(activity="Thinking", spinner_frame=0)
    )
    thinking_1 = "".join(
        item[1] for item in prompt_footer(activity="Thinking", spinner_frame=1)
    )
    running = "".join(
        item[1] for item in prompt_footer(activity="Running read_file", spinner_frame=2)
    )
    assert thinking_0 != thinking_1
    assert "\n⠋ Thinking" in thinking_0
    assert "\n⠙ Thinking" in thinking_1
    assert "Thinking..." in thinking_0
    assert "Running read_file..." in running
    assert "Thinking...\n? /help" in thinking_0


def test_tui_status_row_is_reserved_and_can_be_disabled() -> None:
    idle = "".join(item[1] for item in prompt_footer())
    assert "\n \n? /help" in idle
    state: dict[str, object] = {
        "activity": None,
        "spinner_frame": 0,
        "status_enabled": False,
    }
    _set_activity(state, "Thinking...")
    assert state["activity"] is None


def test_static_result_markers_have_success_and_error_styles() -> None:
    success = result_status("Tool read_file completed", "success")
    failed = result_status("Tool read_file failed", "failed")
    assert success.plain.startswith("● ")
    assert failed.plain.startswith("● ")
    assert success.spans[0].style == "success"
    assert failed.spans[0].style == "error"


def test_tui_renderer_prints_reasoning_tool_status_and_final_answer() -> None:
    async def scenario() -> None:
        console, output = console_buffer()
        state: dict[str, object] = {"activity": None, "spinner_frame": 0}
        renderer = _RunRenderer(_TerminalWriter(console), state)
        await renderer.handle(event(AgentEventKind.THINKING, {}, 1))
        assert state["activity"] == "Thinking..."
        await renderer.handle(
            event(AgentEventKind.THINKING, {"text": "inspect files"}, 2)
        )
        await renderer.handle(event(AgentEventKind.TEXT_DELTA, {"text": "result"}, 3))
        await renderer.handle(
            event(AgentEventKind.TOOL_RUNNING, {"name": "read_file"}, 4)
        )
        assert state["activity"] == "Reading files: read_file..."
        await renderer.handle(
            event(
                AgentEventKind.TOOL_COMPLETED,
                {"name": "read_file", "ok": True, "duration_ms": 8},
                5,
            )
        )
        await renderer.handle(event(AgentEventKind.THINKING, {"text": "compose"}, 6))
        await renderer.handle(event(AgentEventKind.TEXT_DELTA, {"text": " final"}, 7))
        await renderer.handle(
            event(
                AgentEventKind.COMPLETED,
                {
                    "status": "completed",
                    "answer": "result final",
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                    "duration_ms": 9,
                },
                8,
            )
        )
        rendered = output.getvalue()
        assert "◇ Model reasoning" in rendered
        assert "inspect files" in rendered
        assert "● Reasoning complete" in rendered
        assert "● Tool read_file completed · 8ms" in rendered
        assert "◆ CapsLock" in rendered
        assert "Final answer" not in rendered
        assert "result" in rendered and "final" in rendered
        assert "● Completed · run run · 9ms · 2/1 tokens" in rendered
        assert state["activity"] is None

    asyncio.run(scenario())


def test_tui_renderer_uses_distinct_reasoning_and_answer_styles() -> None:
    class RecordingWriter:
        def __init__(self) -> None:
            self.text_styles: list[tuple[str, str | None]] = []

        async def print(self, *args, **kwargs) -> None:
            return None

        async def write_text(self, text: str, *, style: str | None = None) -> None:
            self.text_styles.append((text, style))

        async def flush(self) -> None:
            return None

    async def scenario() -> None:
        writer = RecordingWriter()
        renderer = _RunRenderer(writer, {"activity": None, "spinner_frame": 0})
        await renderer.handle(event(AgentEventKind.THINKING, {"text": "inspect"}, 1))
        await renderer.handle(event(AgentEventKind.TEXT_DELTA, {"text": "answer"}, 2))
        assert writer.text_styles == [
            ("inspect", "reasoning"),
            ("answer", "answer"),
        ]

    asyncio.run(scenario())
