from types import SimpleNamespace

import httpx

from capslock import cli
from capslock.external import TAVILY_SEARCH_URL, WebService
from capslock.observability import EventSink
from capslock.session import SessionStore


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
    monkeypatch.setattr(cli, "_service_for_external", lambda agent, action: service)
    monkeypatch.setattr(cli.console, "input", lambda prompt: "a")

    completed = cli._review_pending_external_actions(agent, "run")

    assert [item.id for item in completed] == [action.id]
    assert agent.store.get_external_action(action.id).status == "completed"
    assert agent.store.list_sources("session")[0].title == "Schedule"


def test_pending_external_action_can_be_rejected_inline(tmp_path, monkeypatch) -> None:
    agent, action, service = _setup(tmp_path)
    monkeypatch.setattr(cli, "_service_for_external", lambda agent, action: service)
    monkeypatch.setattr(cli.console, "input", lambda prompt: "r")

    completed = cli._review_pending_external_actions(agent, "run")

    assert completed == []
    assert agent.store.get_external_action(action.id).status == "rejected"


def test_chat_turn_continues_after_approved_web_action(monkeypatch) -> None:
    questions: list[str] = []
    answers = [SimpleNamespace(run_id="first"), SimpleNamespace(run_id="second")]
    agent = SimpleNamespace(ask=lambda question: questions.append(question) or answers.pop(0))
    reviews = [[SimpleNamespace(kind="web_search")], []]
    monkeypatch.setattr(cli, "_render", lambda answer, debug: None)
    monkeypatch.setattr(cli, "_render_changes", lambda agent, pending_only=False: None)
    monkeypatch.setattr(cli, "_review_pending_external_actions", lambda agent, run_id: reviews.pop(0))

    cli._run_chat_turn(agent, "Search the schedule", False)

    assert questions[0] == "Search the schedule"
    assert "list_external_sources" in questions[1]
    assert "Do not propose the same search again" in questions[1]


def test_arrow_navigation_wraps_selection() -> None:
    assert cli._move_selection(0, "down", 3) == 1
    assert cli._move_selection(0, "up", 3) == 2
    assert cli._move_selection(2, "right", 3) == 0
    assert cli._move_selection(1, "left", 3) == 0
    assert cli._move_selection(1, "unknown", 3) == 1
