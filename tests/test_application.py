import sqlite3
from pathlib import Path

import pytest

from capslock.application import ActionCoordinator
from capslock.application.app import WorkspaceApplication
from capslock.config import CommandSettings, McpSettings, ModelSettings, RuntimeSettings, Settings, WebSettings
from capslock.domain import ActionResultKind, ActionStatus, ActionType
from capslock.model import ModelMessage, ModelResponse
from capslock.permissions import PermissionMode
from capslock.policy import WorkspacePolicy
from capslock.session import SessionStore


class FakeModel:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.closed = False

    def complete(self, **kwargs) -> ModelResponse:
        return ModelResponse(ModelMessage(self.responses.pop(0)))

    def close(self) -> None:
        self.closed = True


def settings() -> Settings:
    return Settings(
        ModelSettings("key", "https://example.com", "test", 10, 0, 0),
        RuntimeSettings(6, 24),
        CommandSettings(10, 1000),
        WebSettings(None, 10, 1000, 2),
        McpSettings(10, 1000),
        PermissionMode.APPROVE_FOR_ME.value,
    )


def test_action_coordinator_owns_approval_execution_and_rejection(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path / "state.sqlite3")
    events = []
    coordinator = ActionCoordinator(
        store=store,
        policy=WorkspacePolicy(tmp_path),
        session_id="session",
        run_id="run",
        event=lambda kind, **data: events.append((kind, data)),
        permission_mode=PermissionMode.ASK_FOR_APPROVAL,
    )

    pending = coordinator.propose(ActionType.FILE_EDIT, path="note.txt", old_text="before", new_text="after")
    assert pending.status is ActionStatus.PENDING
    completed = coordinator.approve_and_execute(pending.id)
    assert completed.status is ActionStatus.COMPLETED
    assert completed.result_kind is ActionResultKind.APPLIED
    assert store.get_action(pending.id).started_at is not None

    rejected = coordinator.propose(ActionType.FILE_CREATE, path="other.txt", content="content")
    assert coordinator.reject(rejected.id).status is ActionStatus.REJECTED
    with pytest.raises(ValueError, match="not pending"):
        coordinator.approve_and_execute(rejected.id)


def test_workspace_application_closes_model_and_database(tmp_path: Path) -> None:
    model = FakeModel(["done"])
    application = WorkspaceApplication(workspace=tmp_path, settings=settings(), client=model)
    assert application.agent.ask("question").text == "done"
    connection = application.store._connection

    application.close()

    assert model.closed
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_workspace_application_closes_resources_when_agent_creation_fails(tmp_path: Path) -> None:
    with SessionStore(tmp_path / ".capslock" / "capslock.sqlite3") as store:
        session_id = store.create(tmp_path / "other", "test").id
    model = FakeModel()

    with pytest.raises(Exception, match="different workspace"):
        WorkspaceApplication(workspace=tmp_path, settings=settings(), client=model, session_id=session_id)

    assert model.closed
