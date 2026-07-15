from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.execution import CommandService, CommandTemplate, TEMPLATES
from capslock.policy import PolicyError, WorkspacePolicy
from capslock.runtime import WorkspaceAgent
from capslock.session import SessionStore
from capslock.tools import RunContext, workspace_tools


def service(tmp_path: Path, session_id: str = "session", *, timeout: float = 10, output_limit: int = 100_000) -> CommandService:
    store = SessionStore(tmp_path / ".capslock" / "capslock.sqlite3")
    return CommandService(store, WorkspacePolicy(tmp_path), session_id, "run", lambda *args, **kwargs: None, timeout_seconds=timeout, output_limit_bytes=output_limit)


def python_workspace(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='example'\nversion='0.0.0'\n", encoding="utf-8")


def test_command_requires_approval_and_records_result(tmp_path: Path) -> None:
    python_workspace(tmp_path)
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    commands = service(tmp_path)
    proposal = commands.propose("pytest", target="test_ok.py")
    assert proposal.status == "pending"
    with pytest.raises(ValueError, match="explicit approval"):
        commands.execute(proposal.id)
    commands.approve(proposal.id)
    completed = commands.execute(proposal.id)
    assert completed.status == "completed"
    assert completed.exit_code == 0


def test_command_rejects_unsafe_cwd_and_cross_session_execution(tmp_path: Path) -> None:
    python_workspace(tmp_path)
    commands = service(tmp_path, "one")
    with pytest.raises(PolicyError, match="allowed workspace"):
        commands.propose("pytest", cwd="..")
    proposal = commands.propose("pytest")
    with pytest.raises(PolicyError, match="does not belong"):
        service(tmp_path, "two").approve(proposal.id)


def test_command_timeout_and_output_truncation(tmp_path: Path, monkeypatch) -> None:
    python_workspace(tmp_path)
    import sys
    monkeypatch.setitem(TEMPLATES, "test_sleep", CommandTemplate("test_sleep", "Sleep", (sys.executable, "-c", "import time; time.sleep(1)")))
    monkeypatch.setitem(TEMPLATES, "test_output", CommandTemplate("test_output", "Output", (sys.executable, "-c", "print('x' * 1000)")))
    timed = service(tmp_path, timeout=0.01)
    proposal = timed.propose("test_sleep")
    timed.approve(proposal.id)
    assert timed.execute(proposal.id).status == "failed"
    output = service(tmp_path, output_limit=20)
    proposal = output.propose("test_output")
    output.approve(proposal.id)
    result = output.execute(proposal.id)
    assert result.status == "completed"
    row = output.store._connection.execute("SELECT output_truncated FROM command_action_data WHERE action_id=?", (proposal.id,)).fetchone()
    assert row[0] == 1


def test_command_tool_cannot_bypass_approval(tmp_path: Path) -> None:
    python_workspace(tmp_path)
    store = SessionStore(tmp_path / ".capslock" / "capslock.sqlite3")
    context = RunContext(session_id="session", run_id="run", policy=WorkspacePolicy(tmp_path), event=lambda *args, **kwargs: None, store=store)
    proposed, _ = workspace_tools().invoke("propose_command", context, {"template": "pytest"})
    executed, _ = workspace_tools().invoke("run_command", context, {"command_id": proposed.data["command_id"]})
    assert not executed.ok and "explicit approval" in executed.error


class Responses:
    def __init__(self, responses): self.responses = list(responses)
    def create(self, **kwargs): return self.responses.pop(0)


class Client:
    def __init__(self, responses): self.chat = SimpleNamespace(completions=Responses(responses))


def test_runtime_records_usage_cost_tasks_and_compacted_context(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    session = store.create(tmp_path, "test")
    store.append_message(session.id, "user", "old question")
    store.append_message(session.id, "assistant", "old answer")
    store.replace_tasks(session.id, ["verify output"])
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=[]))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    agent = WorkspaceAgent(Client([response]), workspace=tmp_path, model="test", store=store, session_id=session.id, max_context_messages=1, input_cost_per_million=2, output_cost_per_million=4)
    assert agent.ask("continue").text == "done"
    assert store.session_cost(session.id) == (10, 5, 0.00004)
    assert store.get(session.id).updated_at
    task = store.list_tasks(session.id)[0]
    assert store.update_task_status(task.id, session.id, "completed").status == "completed"
