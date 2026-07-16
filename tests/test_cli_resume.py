from contextlib import contextmanager
from types import SimpleNamespace

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from capslock.cli import app as cli
from capslock.cli import prompt
from capslock.session import SessionStore
from capslock.theme import make_console


def session(identifier: str, title: str, updated_at: str) -> SimpleNamespace:
    return SimpleNamespace(id=identifier, title=title, updated_at=updated_at)


def test_session_selector_uses_arrow_keys_and_enter() -> None:
    sessions = [
        session("a" * 32, "First session", "2026-07-15T10:00:00+00:00"),
        session("b" * 32, "第二个会话", "2026-07-16T11:30:00+00:00"),
    ]

    with create_pipe_input() as pipe:
        pipe.send_text("\x1b[B\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            selected = prompt.select_session(sessions, width=120)

    assert selected == "b" * 32


def test_session_selector_rows_include_title_updated_time_and_id(monkeypatch) -> None:
    saved = session("a" * 32, "自然语言标题", "2026-07-16T11:30:00+00:00")
    captured = {}

    def choose(message, **kwargs):
        captured["message"] = message
        captured["options"] = kwargs["options"]
        return kwargs["options"][0][0]

    monkeypatch.setattr(prompt, "choice", choose)

    assert prompt.select_session([saved], width=120) == saved.id
    header = "".join(fragment[1] for fragment in captured["message"])
    row = "".join(fragment[1] for fragment in captured["options"][0][1])
    assert "Title" in header and "Updated (UTC)" in header and "Session ID" in header
    assert "自然语言标题" in row
    assert "2026-07-16 11:30" in row
    assert saved.id in row


def test_session_selector_columns_align_with_choice_prefix(monkeypatch) -> None:
    sessions = [
        session("a" * 32, "Short", "2026-07-15T10:00:00+00:00"),
        session("b" * 32, "包含中文的会话标题", "2026-07-16T11:30:00+00:00"),
    ]
    captured = {}

    def choose(message, **kwargs):
        captured["message"] = message
        captured["options"] = kwargs["options"]
        return kwargs["options"][0][0]

    monkeypatch.setattr(prompt, "choice", choose)
    prompt.select_session(sessions, width=80)

    header = "".join(fragment[1] for fragment in captured["message"]).splitlines()[1]
    rows = ["".join(fragment[1] for fragment in option[1]) for option in captured["options"]]
    absolute_header = " " + header
    absolute_rows = [" " * 3 + "❯ " + f"{index:2d}. " + row for index, row in enumerate(rows, 1)]
    expected_columns = [
        get_cwidth(absolute_header[: absolute_header.index(label)])
        for label in ("Title", "Updated (UTC)", "Session ID")
    ]

    for absolute_row, saved in zip(absolute_rows, sessions, strict=True):
        columns = [
            get_cwidth(absolute_row[: absolute_row.index(value)])
            for value in (saved.title, "2026-07", saved.id)
        ]
        assert columns == expected_columns
        assert get_cwidth(absolute_row) <= 80


def test_bare_resume_uses_interactive_session_selection(tmp_path, monkeypatch) -> None:
    with SessionStore(tmp_path / ".capslock" / "capslock.sqlite3") as store:
        first = store.create(tmp_path, "test")
        second = store.create(tmp_path, "test")
    selected = []
    resumed = []

    def select_saved_session(console, store, limit):
        selected.append((len(store.list(limit)), limit))
        return second.id

    @contextmanager
    def create_application(workspace, settings, session_id=None, *, layout=None):
        resumed.append(session_id)
        yield SimpleNamespace(agent=object())

    monkeypatch.setattr(cli, "load_project_environment", lambda workspace: None)
    monkeypatch.setattr(cli.Settings, "load", lambda workspace, *, layout=None: object())
    monkeypatch.setattr(cli, "select_saved_session", select_saved_session)
    monkeypatch.setattr(cli, "create_application", create_application)
    monkeypatch.setattr(cli, "run_chat", lambda context, debug: 0)

    assert cli.main(["--workspace", str(tmp_path), "resume", "--limit", "1"]) == 0
    assert selected == [(1, 1)]
    assert resumed == [second.id]
    assert first.id != second.id


def test_bare_resume_with_no_sessions_exits_without_creating_one(tmp_path, monkeypatch) -> None:
    terminal = make_console(width=100, color_system=None, force_terminal=False, record=True)
    monkeypatch.setattr(cli, "load_project_environment", lambda workspace: None)
    monkeypatch.setattr(cli.Settings, "load", lambda workspace, *, layout=None: object())

    assert cli.main(["--workspace", str(tmp_path), "resume"], console=terminal) == 0
    assert "No saved sessions in this workspace" in terminal.export_text()

    with SessionStore(tmp_path / ".capslock" / "capslock.sqlite3") as store:
        assert store.list() == []
