import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.domain import MAX_SESSION_TITLE_LENGTH, SessionTitleSource
from capslock.model import ModelMessage, ModelResponse
from capslock.observability import EventSink
from capslock.policy import WorkspacePolicy
from capslock.runtime import WorkspaceAgent
from capslock.session import SessionStore
from capslock.tools import RunContext, workspace_tools


class Responses:
    def __init__(self, responses): self.responses = list(responses)
    def create(self, **kwargs): return self.responses.pop(0)


class Client:
    def __init__(self, responses): self.chat = SimpleNamespace(completions=Responses(responses))


def response(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls or []))])


def test_tools_enforce_workspace_and_return_stable_evidence(tmp_path: Path) -> None:
    (tmp_path / "src.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    context = RunContext(session_id="s", run_id="r", policy=WorkspacePolicy(tmp_path), event=lambda *args, **kwargs: None)
    result, _ = workspace_tools().invoke("read_file", context, {"path": "src.py"})
    assert result.ok and result.citations[0].id.startswith("ev_")
    rejected, _ = workspace_tools().invoke("read_file", context, {"path": "../secret.txt"})
    assert not rejected.ok and "allowed workspace" in rejected.error


def test_runtime_recovers_from_bad_tool_arguments_and_persists_session(tmp_path: Path) -> None:
    bad = SimpleNamespace(id="bad", function=SimpleNamespace(name="read_file", arguments="not json"))
    store = SessionStore(tmp_path / ".capslock" / "capslock.sqlite3")
    answer = WorkspaceAgent(Client([response(tool_calls=[bad]), response("I could not read that file.")]), workspace=tmp_path, model="test", store=store).ask("Read something")
    assert answer.text == "I could not read that file."
    assert [message["role"] for message in store.messages(answer.session_id)] == ["user", "assistant"]


def test_runtime_citation_uses_evidence_id_and_resume(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("alpha line\nimportant fact\n", encoding="utf-8")
    call = SimpleNamespace(id="search", function=SimpleNamespace(name="search_files", arguments=json.dumps({"path": ".", "query": "important"})))
    evidence_id = "ev_" + sha256(f"{path.resolve()}:1:2".encode()).hexdigest()[:16]
    store = SessionStore(tmp_path / "state.sqlite3")
    answer = WorkspaceAgent(Client([response(tool_calls=[call]), response(f"The fact is present. [[evidence:{evidence_id}]]")]), workspace=tmp_path, model="test", store=store).ask("Find important")
    assert answer.citations[0].path == path.resolve()
    resumed = WorkspaceAgent(Client([response("Still here.")]), workspace=tmp_path, model="test", store=store, session_id=answer.session_id)
    assert resumed.ask("Continue").text == "Still here."


def test_session_title_uses_first_question_until_manually_renamed(tmp_path: Path) -> None:
    class Model:
        def complete(self, **kwargs) -> ModelResponse:
            return ModelResponse(ModelMessage("done"))

    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(Model(), workspace=tmp_path, model="test", store=store)
    created = store.get(agent.session_id)
    assert created is not None and created.title
    assert created.title_source is SessionTitleSource.PENDING

    agent.ask("  First\nquestion " + "x" * 100)
    automatic = store.get(agent.session_id)
    assert automatic is not None
    assert automatic.title.startswith("First question")
    assert len(automatic.title) == MAX_SESSION_TITLE_LENGTH
    assert automatic.title_source is SessionTitleSource.FIRST_QUESTION

    renamed = store.rename_session(agent.session_id, "  Release   planning  ")
    assert renamed.title == "Release planning"
    assert renamed.title_source is SessionTitleSource.MANUAL
    agent.ask("This must not replace the manual title")
    assert store.get(agent.session_id).title == "Release planning"

    with pytest.raises(ValueError, match="cannot be empty"):
        store.rename_session(agent.session_id, " \n ")
    with pytest.raises(ValueError, match="cannot exceed"):
        store.rename_session(agent.session_id, "x" * (MAX_SESSION_TITLE_LENGTH + 1))


def test_session_rejects_wrong_workspace(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    session = store.create(tmp_path, "test")
    with pytest.raises(Exception, match="different workspace"):
        WorkspaceAgent(Client([]), workspace=tmp_path / "other", model="test", store=store, session_id=session.id)


def test_internal_model_protocol_and_events_are_scoped_per_run(tmp_path: Path) -> None:
    class Model:
        def __init__(self) -> None:
            self.answers = ["first", "second"]

        def complete(self, **kwargs) -> ModelResponse:
            return ModelResponse(ModelMessage(self.answers.pop(0)))

    sink = EventSink()
    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(Model(), workspace=tmp_path, model="test", store=store, event_sink=sink)

    first = agent.ask("one")
    second = agent.ask("two")

    assert first.text == "first" and second.text == "second"
    assert [event.kind for event in first.events] == ["run_started"]
    assert [event.kind for event in second.events] == ["run_started"]
    assert first.events[0].data["run_id"] != second.events[0].data["run_id"]


def test_runtime_records_unexpected_model_failure(tmp_path: Path) -> None:
    class FailingModel:
        def complete(self, **kwargs):
            raise RuntimeError("model failed")

    store = SessionStore(tmp_path / "state.sqlite3")
    agent = WorkspaceAgent(FailingModel(), workspace=tmp_path, model="test", store=store)

    with pytest.raises(RuntimeError, match="model failed"):
        agent.ask("question")

    row = store._connection.execute(
        "SELECT status,error FROM runs WHERE session_id=?", (agent.session_id,)
    ).fetchone()
    assert row["status"] == "failed" and row["error"] == "model failed"
