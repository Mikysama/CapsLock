from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from capslock.cli.context import CliContext
from capslock.cli.app import _ui_mode, build_parser
from capslock.cli.commands import COMMANDS
from capslock.cli.fullscreen_tui.app import CSS, CapsLockApp, run_fullscreen_tui
from capslock.cli.fullscreen_tui.models import (
    MessageKind,
    MessageViewModel,
    TuiState,
    reduce_event,
    toggle_details,
)
from capslock.cli.fullscreen_tui.presentation import present_action
from capslock.cli.fullscreen_tui.rendering import TransparentBackground
from capslock.cli.fullscreen_tui.screens import ApprovalScreen, ModelScreen
from capslock.cli.fullscreen_tui.widgets import (
    ActivityBar,
    CompletionBar,
    Composer,
    MessageWidget,
    SessionHeader,
    TranscriptView,
)
from capslock.domain import (
    ActionRecord,
    ActionStatus,
    ActionType,
    AgentEvent,
    AgentEventKind,
    ApprovalDecision,
    SessionInfo,
    SessionTitleSource,
)
from capslock.permissions import PermissionMode
from capslock.theme import make_console
from capslock.tooling.presentation import tool_presentation


def _event(kind: AgentEventKind, data: dict, sequence: int = 1) -> AgentEvent:
    return AgentEvent(sequence, "now", "session", "run", "work", kind, data)


def test_tool_presentation_is_allowlisted_and_redacted() -> None:
    view = tool_presentation(
        "mcp__local__lookup",
        {"password": "do-not-emit", "payload": "private"},
    )
    assert view == {
        "version": 1,
        "category": "mcp",
        "title": "MCP local lookup",
        "detail": "mcp__local__lookup",
    }
    assert "do-not-emit" not in str(view)
    assert "private" not in str(view)


def test_action_preview_is_redacted_and_bounded() -> None:
    diff = "\n".join(
        ["+password=secret-value", *[f"+line {index}" for index in range(80)]]
    )
    action = ActionRecord(
        "action",
        "session",
        "run",
        ActionType.FILE_EDIT,
        ActionStatus.PENDING,
        "Update config",
        {"path": "config.py", "diff": diff},
        None,
        None,
        "now",
        risk_level="high",
    )
    view = present_action(action)
    assert view.target == "config.py"
    assert "secret-value" not in (view.preview or "")
    assert "<redacted>" in (view.preview or "")
    assert len((view.preview or "").splitlines()) <= 41
    assert (view.preview or "").endswith("preview truncated")


def test_reducer_groups_read_tools_and_collapses_completed_reasoning() -> None:
    state = reduce_event(
        TuiState(), _event(AgentEventKind.THINKING, {"text": "inspect"}, 1)
    )
    state = reduce_event(
        state,
        _event(
            AgentEventKind.TOOL_RUNNING,
            {
                "name": "read_file",
                "tool_call_id": "one",
                "presentation": tool_presentation("read_file", {"path": "a.py"}),
            },
            2,
        ),
    )
    state = reduce_event(
        state,
        _event(
            AgentEventKind.TOOL_RUNNING,
            {
                "name": "search_files",
                "tool_call_id": "two",
                "presentation": tool_presentation(
                    "search_files", {"path": ".", "query": "needle"}
                ),
            },
            3,
        ),
    )
    reasoning = next(
        item for item in state.messages if item.kind is MessageKind.REASONING
    )
    tools = next(item for item in state.messages if item.kind is MessageKind.TOOLS)
    assert reasoning.collapsed is True
    assert tools.collapsed is True
    assert len(tools.tools) == 2
    expanded = toggle_details(state)
    assert all(
        not item.collapsed
        for item in expanded.messages
        if item.kind in {MessageKind.REASONING, MessageKind.TOOLS}
    )


def test_reducer_keeps_paused_run_resumable() -> None:
    state = reduce_event(
        TuiState(),
        _event(
            AgentEventKind.TOOL_RUNNING,
            {"name": "ask_user", "tool_call_id": "ask"},
        ),
    )
    state = reduce_event(
        state,
        _event(AgentEventKind.WAITING_INPUT, {"request_id": "input"}, 2),
    )
    assert "run" not in state.terminal_runs
    assert state.activity == "Waiting for input"
    assert state.messages[-1].tools[0].status == "waiting_input"
    resumed = reduce_event(
        state,
        _event(AgentEventKind.THINKING, {"text": "resume"}, 3),
    )
    assert resumed.messages[-1].text == "resume"


def test_transparent_background_preserves_font_style() -> None:
    console = make_console(width=40)
    source_style = Style(
        color="#AABBCC",
        bgcolor="#000000",
        bold=True,
        dim=True,
        italic=True,
        underline=True,
        strike=True,
        link="https://example.test",
    )
    rendered = list(
        console.render(TransparentBackground(Text("styled", style=source_style)))
    )
    style = next(segment.style for segment in rendered if segment.text == "styled")
    assert style is not None
    assert style.color == source_style.color
    assert style.bold == source_style.bold
    assert style.dim == source_style.dim
    assert style.italic == source_style.italic
    assert style.underline == source_style.underline
    assert style.strike == source_style.strike
    assert style.link == source_style.link
    assert style.bgcolor is not None and style.bgcolor.is_default


class _Sessions:
    def __init__(self, transcript: list[dict] | None = None) -> None:
        self.entries = transcript or []

    async def require(self, session_id: str) -> SessionInfo:
        return SessionInfo(
            session_id,
            Path("."),
            "test-model",
            "now",
            "now",
            "Test session",
            SessionTitleSource.MANUAL,
        )

    async def transcript(self, session_id: str) -> list[dict]:
        return self.entries

    async def delete_if_empty(self, session_id: str) -> bool:
        return False


class _Model:
    def set_budget_authorizer(self, value) -> None:
        self.authorizer = value


class _Skills:
    def entries(self) -> list[object]:
        return []


class _Queries:
    def __init__(self, sessions: _Sessions | None = None) -> None:
        self.sessions = sessions or _Sessions()

    async def session(self, session_id: str):
        return await self.sessions.require(session_id)

    async def transcript(self, session_id: str) -> list[dict]:
        return await self.sessions.transcript(session_id)


class _Agent:
    session_id = "s" * 32
    workspace = Path(".")
    model = "test-model"
    permission_mode = PermissionMode.APPROVE_FOR_ME
    memory = None
    context_budget = SimpleNamespace(input_budget=128_000)

    def __init__(self, events: list[AgentEvent] | None = None) -> None:
        self.queries = _Queries()
        self.chat_model = _Model()
        self.skills = _Skills()
        self.events = events or []
        self.action_authorizer = None

    def set_action_authorizer(self, value) -> None:
        self.action_authorizer = value

    async def enqueue(self, question: str):
        return SimpleNamespace(id="work-item", question=question)

    async def set_model(self, value: str) -> str:
        from capslock.models import selectable_model

        self.model = selectable_model(value)
        return self.model

    async def delete_if_empty(self) -> bool:
        return await self.queries.sessions.delete_if_empty(self.session_id)

    async def run_stream(self, request):
        for item in self.events:
            yield item


@pytest.mark.parametrize("size", [(120, 32), (80, 24), (60, 20)])
def test_fullscreen_layout_at_supported_sizes(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = CapsLockApp(CliContext(make_console(), _Agent()))
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert app.query_one(SessionHeader).display
            assert app.query_one(TranscriptView).display
            assert app.query_one(Composer).has_focus

    asyncio.run(scenario())


def test_fullscreen_activity_timer_only_runs_while_spinner_is_visible() -> None:
    async def scenario() -> None:
        app = CapsLockApp(CliContext(make_console(), _Agent()))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app._activity_timer is not None
            assert not app._activity_timer._active.is_set()

            app.state = replace(app.state, activity="Thinking")
            await app._sync()
            assert app._activity_timer._active.is_set()

            app.state = replace(app.state, activity=None)
            await app._sync()
            assert not app._activity_timer._active.is_set()

    asyncio.run(scenario())


def test_fullscreen_activity_bar_does_not_repaint_when_already_clear() -> None:
    bar = ActivityBar()
    updates: list[object] = []
    bar.update = updates.append  # type: ignore[method-assign]

    active = replace(TuiState(), activity="Thinking")
    assert bar.update_state(active, enabled=True)
    assert not bar.update_state(TuiState(), enabled=True)
    assert not bar.update_state(TuiState(), enabled=True)
    assert len(updates) == 2


def test_fullscreen_message_widget_skips_unchanged_updates() -> None:
    async def scenario() -> None:
        app = CapsLockApp(CliContext(make_console(), _Agent()))
        async with app.run_test(size=(80, 24)):
            message = MessageViewModel("message", MessageKind.USER, "hello")
            widget = MessageWidget(message)
            updates: list[object] = []
            widget.update = updates.append  # type: ignore[method-assign]

            widget.update_message(message)
            widget.update_message(replace(message, text="updated"))
            assert len(updates) == 1

    asyncio.run(scenario())


def test_fullscreen_user_prompt_background_fills_transcript_width() -> None:
    async def scenario() -> None:
        agent = _Agent()
        agent.queries.sessions = _Sessions([{"role": "user", "content": "hello"}])
        app = CapsLockApp(CliContext(make_console(), agent))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            transcript = app.query_one(TranscriptView)
            message = app.query_one(MessageWidget)

            assert message.outer_size.width == transcript.content_region.width
            assert message.styles.background.hex == "#E0E0E0"

    asyncio.run(scenario())


def test_fullscreen_composer_input_background_is_transparent() -> None:
    async def scenario() -> None:
        app = CapsLockApp(CliContext(make_console(), _Agent()))
        async with app.run_test(size=(80, 24)) as pilot:
            composer = app.query_one(Composer)
            composer.load_text("user input")
            composer.cursor_location = (0, 0)
            await pilot.pause()

            input_segments = [
                segment for segment in composer.render_line(0) if segment.text.strip()
            ]
            assert input_segments
            assert all(
                segment.style is not None
                and segment.style.bgcolor is not None
                and segment.style.bgcolor.is_default
                for segment in input_segments
            )

    asyncio.run(scenario())


def test_fullscreen_markdown_character_backgrounds_are_transparent() -> None:
    async def scenario() -> None:
        app = CapsLockApp(CliContext(make_console(), _Agent()))
        async with app.run_test(size=(80, 24)) as pilot:
            message = MessageWidget(
                MessageViewModel(
                    id="styled-answer",
                    kind=MessageKind.ASSISTANT,
                    text="**bold** `inline`\n\n```python\nprint(1)\n```",
                )
            )
            await app.screen.mount(message)
            await pilot.pause()

            segments = [
                segment
                for y in range(message.content_region.height)
                for segment in message.render_line(y)
                if segment.text.strip()
            ]
            assert segments
            assert all(
                segment.style is not None
                and segment.style.bgcolor is not None
                and segment.style.bgcolor.is_default
                for segment in segments
            )

    asyncio.run(scenario())


def test_fullscreen_submit_streams_answer_and_keeps_composer_fixed() -> None:
    events = [
        _event(AgentEventKind.QUEUED, {"status": "running"}, 1),
        _event(AgentEventKind.THINKING, {"text": "检查"}, 2),
        _event(AgentEventKind.TEXT_DELTA, {"text": "完成 ✅"}, 3),
        _event(
            AgentEventKind.COMPLETED,
            {
                "answer": "完成 ✅",
                "status": "completed",
                "usage": {"input_tokens": 2, "output_tokens": 1},
                "duration_ms": 5,
            },
            4,
        ),
    ]

    async def scenario() -> None:
        app = CapsLockApp(CliContext(make_console(), _Agent(events)))
        async with app.run_test(size=(80, 24)) as pilot:
            composer = app.query_one(Composer)
            composer.load_text("请检查")
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert composer.text == ""
            assert any(
                item.kind is MessageKind.ASSISTANT and item.text == "完成 ✅"
                for item in app.state.messages
            )
            assert app.state.queue == ()
            assert composer.region.y > app.query_one(TranscriptView).region.y

    asyncio.run(scenario())


def test_fullscreen_command_menu_is_vertical_complete_and_scrolls_selection() -> None:
    async def scenario() -> None:
        app = CapsLockApp(CliContext(make_console(), _Agent()))
        async with app.run_test(size=(80, 24)) as pilot:
            composer = app.query_one(Composer)
            composer.load_text("/")
            await pilot.pause()

            menu = app.query_one(CompletionBar)
            rendered = menu.query_one(".completion-content", Static).render().plain
            assert rendered.splitlines() == [
                f"{'❯' if index == 0 else ' '} {item.path.ljust(12)}  {item.description}"
                for index, item in enumerate(COMMANDS)
            ]
            assert "/quit" in rendered
            assert menu.max_scroll_y > 0

            await pilot.press("up")
            await pilot.pause()
            assert app._completion_index == len(COMMANDS) - 1
            assert menu.scroll_y > 0

    asyncio.run(scenario())


def test_fullscreen_model_command_uses_model_dialog() -> None:
    async def scenario() -> None:
        agent = _Agent()
        app = CapsLockApp(CliContext(make_console(), agent))
        async with app.run_test(size=(80, 24)) as pilot:
            composer = app.query_one(Composer)
            composer.load_text("/model")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ModelScreen)

            await pilot.press("down", "enter")
            await pilot.pause()
            assert agent.model == "deepseek-v4-pro"
            assert not isinstance(app.screen, ModelScreen)

    asyncio.run(scenario())


def test_long_transcript_only_mounts_latest_page() -> None:
    transcript = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"消息 {index}"}
        for index in range(10_000)
    ]

    async def scenario() -> None:
        agent = _Agent()
        agent.queries.sessions = _Sessions(transcript)
        app = CapsLockApp(CliContext(make_console(), agent))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert len(app.state.messages) == 10_000
            assert len(app.query(MessageWidget)) == TranscriptView.PAGE_SIZE
            assert "消息 9999" in app.state.messages[-1].text

    asyncio.run(scenario())


def test_new_stream_content_does_not_snap_scrolled_transcript_to_bottom() -> None:
    transcript = [
        {"role": "user", "content": f"long historical message {index} " * 4}
        for index in range(100)
    ]

    async def scenario() -> None:
        agent = _Agent()
        agent.queries.sessions = _Sessions(transcript)
        app = CapsLockApp(CliContext(make_console(), agent))
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(TranscriptView)
            await pilot.pause()
            view.scroll_home(animate=False)
            await pilot.pause()
            assert not view.is_vertical_scroll_end
            app.state = reduce_event(
                app.state,
                _event(AgentEventKind.THINKING, {"text": "new reasoning"}, 101),
            )
            await app._sync()
            await pilot.pause()
            assert not view.is_vertical_scroll_end

    asyncio.run(scenario())


def test_approval_escape_rejects_and_never_defaults_to_execute() -> None:
    action = ActionRecord(
        "action",
        "session",
        "run",
        ActionType.COMMAND,
        ActionStatus.PENDING,
        "Run tests",
        {"argv": ["pytest", "--token=do-not-show"], "cwd": "."},
        None,
        None,
        "now",
        risk_level="high",
    )

    async def scenario() -> None:
        app = CapsLockApp(CliContext(make_console(), _Agent()))
        async with app.run_test(size=(80, 24)) as pilot:
            pending = asyncio.create_task(app._authorize_action(action))
            await pilot.pause()
            assert isinstance(app.screen, ApprovalScreen)
            assert "do-not-show" not in app.export_screenshot()
            preview = app.screen.query_one(".approval-preview Static", Static)
            preview_segments = [
                segment
                for y in range(preview.content_region.height)
                for segment in preview.render_line(y)
                if segment.text.strip()
            ]
            assert preview_segments
            assert all(
                segment.style is not None
                and segment.style.bgcolor is not None
                and segment.style.bgcolor.is_default
                for segment in preview_segments
            )
            await pilot.press("escape")
            assert await pending is ApprovalDecision.REJECT

    asyncio.run(scenario())


def test_ui_mode_flag_and_environment_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAPSLOCK_UI", raising=False)
    assert _ui_mode(build_parser().parse_args([])) == "inline"
    monkeypatch.setenv("CAPSLOCK_UI", "fullscreen")
    assert _ui_mode(build_parser().parse_args([])) == "fullscreen"
    assert _ui_mode(build_parser().parse_args(["--ui", "inline"])) == "inline"
    assert _ui_mode(build_parser().parse_args(["--ui", "fullscreen"])) == "fullscreen"
    for removed in ("modern", "classic"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--ui", removed])
    monkeypatch.setenv("CAPSLOCK_UI", "invalid")
    with pytest.raises(ValueError, match="CAPSLOCK_UI"):
        _ui_mode(build_parser().parse_args([]))


def test_fullscreen_tui_uses_alternate_screen_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: dict[str, object] = {}

    async def run_async(self, **kwargs):
        options.update(kwargs)
        return 0

    monkeypatch.setattr(CapsLockApp, "run_async", run_async)
    context = CliContext(make_console(), _Agent())
    assert asyncio.run(run_fullscreen_tui(context)) == 0
    assert options == {"mouse": True}


def test_fullscreen_tui_only_paints_the_user_prompt_background() -> None:
    background_rules = [
        line.strip() for line in CSS.splitlines() if "background:" in line
    ]
    assert background_rules
    assert all(
        line.endswith(
            (
                "background: transparent;",
                "background: ansi_default;",
                "background: #E0E0E0;",
            )
        )
        for line in background_rules
    )
    assert background_rules.count("background: #E0E0E0;") == 1


def test_fullscreen_tui_root_uses_terminal_default_background() -> None:
    async def scenario() -> None:
        app = CapsLockApp(CliContext(make_console(), _Agent()))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.native_ansi_color
            assert app.styles.background.ansi == -1

    asyncio.run(scenario())
