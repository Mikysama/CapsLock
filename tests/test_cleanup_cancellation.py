"""Durable cancellation must agree across paused runs and queued work."""

import asyncio

import pytest

from capslock.domain import AgentEventKind, WorkItemStatus
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import workspace_run, FakeChatModel, answer
from tests.test_runtime import make_agent


def test_cancel_waiting_work_item_settles_run_and_preserves_usage(tmp_path):
    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repos, "question")
            await repos.workflow.pause(prepared.run.id, kind="user_input", payload={})
            await repos.database.execute(
                "UPDATE runs SET input_tokens=17,output_tokens=3,cost_usd=0.25 WHERE id=?",
                (prepared.run.id,),
            )
            agent = make_agent(
                tmp_path, repos, session.id, FakeChatModel(answer("unused"))
            )
            await agent.cancel_queued_work_item(prepared.work_item.id)
            run = await repos.runs.require(prepared.run.id)
            item = await repos.work_items.require(prepared.work_item.id)
            assert run.status == "cancelled"
            assert item.status is WorkItemStatus.CANCELLED
            assert (run.input_tokens, run.output_tokens, run.cost_usd) == (17, 3, 0.25)
            events = await repos.database.fetch_all(
                "SELECT event_kind FROM run_events WHERE run_id=? ORDER BY sequence",
                (run.id,),
            )
            assert events[-1][0] == AgentEventKind.CANCELLED.value
        finally:
            await repos.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["enter", "submit"])
def test_cancelling_pending_plan_clears_approval_queue(tmp_path, kind):
    from capslock.runtime.model import ModelMessage, ModelResponse, ModelToolCall
    from capslock.planning import PlanningService
    from capslock.runtime import RunRequest
    from capslock.tooling.executor import ToolRuntime
    from capslock.tooling.tools.plans import plan_tools

    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "plan.sqlite3", workspace=tmp_path
        )
        try:
            session = await repos.sessions.create("test-model")
            planning = PlanningService(repos.plans, root=tmp_path / "plans")
            call = ModelToolCall("plan", "enter_plan_mode", '{"objective":"Plan work"}')
            if kind == "submit":
                _, revision = await planning.create(
                    session.id,
                    "Plan work",
                    entry_source="slash",
                    base_permission_mode="approve_for_me",
                    content="# Plan\n\nDo work.\n",
                )
                call = ModelToolCall(
                    "plan",
                    "submit_plan",
                    '{"expected_sha256":"' + revision.sha256 + '"}',
                )
            model = FakeChatModel(ModelResponse(ModelMessage(None, (call,))))
            agent = make_agent(
                tmp_path,
                repos,
                session.id,
                model,
                tools=ToolRuntime(plan_tools()),
                planning=planning,
            )
            events = [
                event async for event in agent.run_stream(RunRequest(question="plan"))
            ]
            paused = events[-1]
            assert paused.kind is AgentEventKind.WAITING_APPROVAL
            await agent.cancel_queued_work_item(paused.work_item_id)
            assert not await agent.plan_requests()
            if kind == "submit":
                active = await repos.plans.active(session.id)
                assert active is not None and active.status.value == "draft"
            run = await repos.runs.require(paused.run_id)
            assert run.status == "cancelled"
        finally:
            await repos.close()

    asyncio.run(scenario())
