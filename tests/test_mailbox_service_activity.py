import asyncio

from capslock.collaboration import (
    AgentTaskContract,
    AgentWorkspaceManager,
    CollaborationService,
    MailboxMessageKind,
)
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import workspace_run


def test_foreground_delegate_yields_on_question_and_keeps_child(tmp_path):
    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / ".capslock/state/capslock.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repo)
            release = asyncio.Event()

            async def child(contract, snapshot):
                await service.send_child_message(
                    contract.task_id,
                    parent_run_id=contract.parent_run_id,
                    kind=MailboxMessageKind.QUESTION,
                    payload={"text": "which file?"},
                )
                await release.wait()
                return {
                    "summary": "done",
                    "evidence": [],
                    "artifacts": [],
                    "checks": [],
                    "memory_proposals": None,
                }

            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(tmp_path),
                repository=repo.collaboration,
                child_runner=child,
            )
            await service.register_mailbox_runtime("session:" + session.id)
            contract = AgentTaskContract.create(prepared.run.id, "inspect")
            try:
                outputs = await asyncio.wait_for(service.delegate([contract]), 10)
                assert outputs[0].state.value == "running"
                assert contract.task_id in service._tasks
                assert service._tasks[contract.task_id].cancelled() is False
                release.set()
                result = await service.wait_foreground(
                    prepared.run.id, asyncio.get_running_loop().time() + 10
                )
                assert result["wake_reason"] == "task_completed"
            finally:
                release.set()
                await service.cancel_foreground(prepared.run.id)
        finally:
            await repo.close()

    asyncio.run(scenario())
