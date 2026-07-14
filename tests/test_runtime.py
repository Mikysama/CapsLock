import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    context = RunContext("s", "r", WorkspacePolicy(tmp_path), 6, lambda *args, **kwargs: None)
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


def test_session_rejects_wrong_workspace(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    session = store.create(tmp_path, "test")
    with pytest.raises(Exception, match="different workspace"):
        WorkspaceAgent(Client([]), workspace=tmp_path / "other", model="test", store=store, session_id=session.id)
