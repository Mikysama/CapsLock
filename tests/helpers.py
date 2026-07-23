from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

from capslock.application.workflow import WorkflowService
from capslock.domain import ActionRecord, ActionResultKind, ActionType
from capslock.runtime.model import ModelMessage, ModelResponse, ModelUsage
from capslock.storage.repositories import WorkspaceRepositories


class FakeChatModel:
    def __init__(self, *responses: ModelResponse | Exception) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, **request: Any) -> ModelResponse:
        self.requests.append(request)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def answer(
    text: str, *, input_tokens: int = 0, output_tokens: int = 0
) -> ModelResponse:
    return ModelResponse(ModelMessage(text), ModelUsage(input_tokens, output_tokens))


class DummySkillRegistry:
    def catalog(self) -> SimpleNamespace:
        return SimpleNamespace(text="")

    def entries(self) -> list[object]:
        return []


class DummySkillService:
    def finish_run(self, run_id: str) -> None:
        return None


class StubActionHandler:
    def __init__(self, types: set[ActionType]) -> None:
        self.types = frozenset(types)

    async def propose(self, action_type: ActionType, payload: dict[str, Any]):
        from capslock.application.action_system import ActionProposal

        return ActionProposal(action_type.value, payload)

    async def execute(self, action: ActionRecord):
        from capslock.application.action_system import ActionExecution

        return ActionExecution({"ok": True}, ActionResultKind.SUCCESS)

    async def revalidate(self, action: ActionRecord):
        return await self.propose(action.type, dict(action.request))

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("not reversible")


async def workspace_run(repositories: WorkspaceRepositories, question: str = "test"):
    session = await repositories.sessions.create("test-model")
    prepared = await workflow_service(repositories).prepare(session.id, question)
    return session, prepared


def workflow_service(repositories: WorkspaceRepositories) -> WorkflowService:
    return WorkflowService(
        repositories.work_items,
        repositories.runs,
        repositories.run_journal,
        repositories.workflow,
    )
