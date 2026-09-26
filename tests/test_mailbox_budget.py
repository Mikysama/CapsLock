import asyncio

import pytest

from capslock.collaboration import AgentTaskContract
from capslock.domain import LoopDetectionSettings, RunLimits, RunMode, RunStopped
from capslock.runtime.governance import RunGovernor
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import workspace_run


def test_child_reservations_guard_parent_and_settle_once(tmp_path):
    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / "db.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repo)
            run_id = prepared.run.id
            team = await repo.collaboration.ensure_default_team(
                session.id, created_by_run_id=run_id
            )
            worker = await repo.collaboration.create_worker(
                team, "child", profile={}, persistent=False
            )
            contract = AgentTaskContract.create(run_id, "work")
            await repo.collaboration.create_task(
                contract, team_id=team, worker_id=worker["id"]
            )
            claim = await repo.collaboration.claim_task(
                contract.task_id,
                worker["id"],
                reservation={"max_tokens": 80, "max_budget_usd": 1},
                account_usage=True,
            )
            governor = await RunGovernor.create(
                repo.governance,
                repo.models,
                run_id,
                parent_run_id=None,
                mode=RunMode.EXEC,
                limits=RunLimits(max_tokens=100, max_budget_usd=1),
                loop_settings=LoopDetectionSettings(),
            )
            governor.attach_collaboration_budget(repo.collaboration)
            await governor.current()
            assert governor.available_remaining()["tokens"] == 20
            with pytest.raises(RunStopped):
                await governor.before_model()
            await repo.collaboration.finish_attempt(
                claim["attempt_id"],
                "completed",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": 0.2,
                    "tool_calls": 1,
                    "tool_rounds": 1,
                },
            )
            await repo.collaboration.finish_attempt(
                claim["attempt_id"],
                "completed",
                usage={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.2},
            )
            first = await governor.current()
            second = await governor.current()
            assert first.tokens == second.tokens == 15
            assert second.cost_usd == 0.2
            assert second.tool_calls == 1
            assert governor.available_remaining()["tokens"] == 85
            restored = await RunGovernor.create(
                repo.governance,
                repo.models,
                run_id,
                parent_run_id=None,
                mode=RunMode.EXEC,
                limits=RunLimits(max_tokens=100, max_budget_usd=1),
                loop_settings=LoopDetectionSettings(),
            )
            restored.attach_collaboration_budget(repo.collaboration)
            assert (await restored.current()).tokens == 15
            assert restored.snapshot.tool_calls == 1

        finally:
            await repo.close()

    asyncio.run(scenario())
