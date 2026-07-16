from types import SimpleNamespace

import httpx
import pytest

from capslock.cli import actions, chat
from capslock.cli.context import CliContext
from capslock.external import TAVILY_SEARCH_URL, WebService
from capslock.cli.prompt import move_selection
from capslock.observability import EventSink
from capslock.permissions import PermissionMode
from capslock.session import SessionStore
from capslock.theme import make_console


def _setup(tmp_path):
    store = SessionStore(tmp_path / "state.sqlite3")
    action = store.create_external_action(
        session_id="session",
        run_id="run",
        kind="web_search",
        payload={"query": "2026 World Cup schedule"},
        summary="Search Tavily for the 2026 World Cup schedule",
    )
    agent = SimpleNamespace(store=store, session_id="session")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(TAVILY_SEARCH_URL)
        return httpx.Response(200, json={"results": [{"url": "https://example.com/schedule", "title": "Schedule", "content": "Match schedule"}]})

    service = WebService(
        store,
        "session",
        "run",
        EventSink().emit,
        tavily_api_key="key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return agent, action, service


def test_pending_external_action_can_be_approved_inline(tmp_path, monkeypatch) -> None:
    agent, action, service = _setup(tmp_path)
    coordinator = SimpleNamespace(
        approve_and_execute=lambda action_id: service.actions.approve(action_id) and service.execute(action_id),
        reject=lambda action_id: service.actions.reject(action_id),
    )
    terminal = make_console(width=100, color_system=None, force_terminal=False, record=True)
    monkeypatch.setattr(actions, "action_coordinator", lambda agent, run_id="cli": coordinator)
    monkeypatch.setattr(terminal, "input", lambda prompt: "a")

    completed = actions.review_pending_external_actions(CliContext(terminal, agent), "run")

    assert [item.id for item in completed] == [action.id]
    assert agent.store.get_external_action(action.id).status == "completed"
    assert agent.store.list_sources("session")[0].title == "Schedule"


def test_pending_external_action_can_be_rejected_inline(tmp_path, monkeypatch) -> None:
    agent, action, service = _setup(tmp_path)
    coordinator = SimpleNamespace(
        approve_and_execute=lambda action_id: service.actions.approve(action_id) and service.execute(action_id),
        reject=lambda action_id: service.actions.reject(action_id),
    )
    terminal = make_console(width=100, color_system=None, force_terminal=False, record=True)
    monkeypatch.setattr(actions, "action_coordinator", lambda agent, run_id="cli": coordinator)
    monkeypatch.setattr(terminal, "input", lambda prompt: "r")

    completed = actions.review_pending_external_actions(CliContext(terminal, agent), "run")

    assert completed == []
    assert agent.store.get_external_action(action.id).status == "rejected"


def test_chat_turn_continues_after_approved_web_action(monkeypatch) -> None:
    questions: list[str] = []
    answers = [SimpleNamespace(run_id="first"), SimpleNamespace(run_id="second")]
    agent = SimpleNamespace(ask=lambda question: questions.append(question) or answers.pop(0))
    reviews = [[SimpleNamespace(kind="web_search")], []]
    terminal = make_console(width=100, color_system=None, force_terminal=False, record=True)
    context = CliContext(terminal, agent)
    monkeypatch.setattr(chat, "render_answer", lambda console, answer, debug: None)
    monkeypatch.setattr(chat.actions, "render_changes", lambda context, pending_only=False: None)
    monkeypatch.setattr(
        chat.actions,
        "review_pending_external_actions",
        lambda context, run_id: reviews.pop(0),
    )

    chat.run_chat_turn(context, "Search the schedule", False)

    assert questions[0] == "Search the schedule"
    assert "list_external_sources" in questions[1]
    assert "Do not propose the same search again" in questions[1]


@pytest.mark.parametrize("command", ["/exit", "/quit"])
def test_chat_exit_deletes_completely_empty_session(tmp_path, monkeypatch, command: str) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    session = store.create(tmp_path, "test")
    agent = SimpleNamespace(
        store=store,
        session_id=session.id,
        memory=None,
        model="test",
        permission_mode=PermissionMode.APPROVE_FOR_ME,
        workspace=tmp_path,
    )
    input_session = SimpleNamespace(prompt=lambda *args, **kwargs: command)
    monkeypatch.setattr(chat, "prompt_session", lambda: input_session)

    assert chat.run_chat(CliContext(make_console(), agent), False) == 0
    assert store.get(session.id) is None


@pytest.mark.parametrize("exception", [EOFError(), KeyboardInterrupt()])
def test_chat_input_exit_deletes_completely_empty_session(tmp_path, monkeypatch, exception) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    session = store.create(tmp_path, "test")
    agent = SimpleNamespace(
        store=store,
        session_id=session.id,
        memory=None,
        model="test",
        permission_mode=PermissionMode.APPROVE_FOR_ME,
        workspace=tmp_path,
    )

    def stop_prompt(*args, **kwargs):
        raise exception

    monkeypatch.setattr(chat, "prompt_session", lambda: SimpleNamespace(prompt=stop_prompt))

    assert chat.run_chat(CliContext(make_console(), agent), False) == 0
    assert store.get(session.id) is None


def test_chat_exit_preserves_nonempty_or_manually_named_session(tmp_path) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    asked = store.create(tmp_path, "test")
    store.start_run(asked.id, "First question")
    renamed = store.create(tmp_path, "test")
    store.rename_session(renamed.id, "Planned work")

    assert not store.delete_session_if_empty(asked.id)
    assert not store.delete_session_if_empty(renamed.id)
    assert store.get(asked.id) is not None
    assert store.get(renamed.id) is not None


def test_chat_exit_preserves_session_with_session_memory(tmp_path, monkeypatch) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    session = store.create(tmp_path, "test")
    memory = SimpleNamespace(list=lambda **kwargs: [object()])
    agent = SimpleNamespace(
        store=store,
        session_id=session.id,
        memory=memory,
        model="test",
        permission_mode=PermissionMode.APPROVE_FOR_ME,
        workspace=tmp_path,
    )
    input_session = SimpleNamespace(prompt=lambda *args, **kwargs: "/exit")
    monkeypatch.setattr(chat, "prompt_session", lambda: input_session)

    assert chat.run_chat(CliContext(make_console(), agent), False) == 0
    assert store.get(session.id) is not None


def test_arrow_navigation_wraps_selection() -> None:
    assert move_selection(0, "down", 3) == 1
    assert move_selection(0, "up", 3) == 2
    assert move_selection(2, "right", 3) == 0
    assert move_selection(1, "left", 3) == 0
    assert move_selection(1, "unknown", 3) == 1
