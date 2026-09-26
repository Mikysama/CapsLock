"""Unknown usage and operational diagnostics without prompt capture."""

import asyncio

from capslock.configuration import ContextSettings
from capslock.runtime.context import ContextBudgetManager
from capslock.runtime.run_support import RunFinalizer
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import FakeChatModel, answer, workspace_run


def test_context_retrieval_distinguishes_empty_and_failure(tmp_path):
    class Memory:
        async def excluded_runs(self):
            return set()

        async def recall_context(self, *args, **kwargs):
            raise RuntimeError("secret prompt must not be stored")

        async def revision_digest(self):
            return ""

    class Episodic:
        async def search(self, *args, **kwargs):
            return []

    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "diag.db", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repos)
            manager = ContextBudgetManager(
                sessions=repos.sessions,
                compactions=repos.compactions,
                settings=ContextSettings(),
                context_window=10000,
                max_output_tokens=100,
                model_profile="test",
                model_name="test",
                tool_schemas=[],
                memory=Memory(),
                episodic=Episodic(),
                performance=repos.performance,
            )
            result = await manager.build(
                session.id,
                "secret query",
                run_id=prepared.run.id,
                instructions="core",
                summarizer=FakeChatModel(answer("unused")),
            )
            assert result is not None
            diagnostics = manager.last_retrieval_diagnostics
            assert diagnostics["memory.recall"]["status"] == "failed"
            assert diagnostics["memory.recall"]["error_type"] == "RuntimeError"
            assert diagnostics["episodic.recall"]["status"] == "empty"
            spans = await repos.performance.trace(prepared.run.id)
            assert any(row["name"] == "context_build" for row in spans)
            assert "secret" not in str(spans)
        finally:
            await repos.close()

    asyncio.run(scenario())


def test_trace_summary_includes_native_model_and_database_time(tmp_path):
    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "summary.db", workspace=tmp_path
        )
        try:
            _, prepared = await workspace_run(repos)
            decision = await repos.models.record_decision(
                prepared.run.id,
                role="reasoning",
                candidates=[],
                selected="test",
                reasons={},
            )
            call = await repos.models.start_call(
                prepared.run.id,
                decision_id=decision,
                role="reasoning",
                profile="test",
                provider="test",
                model="test",
                attempt=1,
                data_policy="shared",
                fallback_from=None,
            )
            await repos.models.finish_call(
                call,
                duration_ms=125,
                input_tokens=1,
                output_tokens=1,
                usage_source="provider",
            )
            rows = await repos.performance.summary()
            model = next(
                row
                for row in rows
                if row["category"] == "model" and row["name"] == "request"
            )
            assert model["total_ms"] == 125
            database = next(
                row
                for row in rows
                if row["category"] == "database" and row["name"] == "commit"
            )
            assert database["samples"] >= 1
            assert database["total_ms"] >= 0
        finally:
            await repos.close()
        reopened = await WorkspaceRepositories.open(
            tmp_path / "summary.db", workspace=tmp_path
        )
        try:
            rows = await reopened.performance.summary()
            assert any(row["category"] == "database" for row in rows)
        finally:
            await reopened.close()

    asyncio.run(scenario())


def test_run_usage_retains_unknown_from_model_summary():
    class ModelSession:
        metered = True

        async def summary(self):
            return [{"calls": 1, "unknown_usage_calls": 1}]

    class Audit:
        async def usage(self, run_id):
            return 0, 0, 0.0

    async def scenario():
        finalizer = RunFinalizer(
            workflow=None,
            journal=None,
            model_audit=Audit(),
            input_cost_per_million=1,
            output_cost_per_million=1,
        )
        usage = await finalizer.usage("run", ModelSession(), 0, 0)
        assert usage.source == "unknown"

    asyncio.run(scenario())


def test_prompt_unknown_usage_is_not_rendered_zero():
    from capslock.cli.prompt import prompt_footer

    value = prompt_footer(width=140, usage=(None, None, None))
    rendered = "".join(text for _, text in value)
    assert "usage unknown" in rendered
    assert "$0.0000" not in rendered


def test_model_selector_lists_unavailable_profile_without_selecting_it(monkeypatch):
    from capslock.cli.prompt import select_model

    captured = {}

    def choose(title, **kwargs):
        captured.update(title=title, **kwargs)
        return kwargs["default"]

    monkeypatch.setattr("capslock.cli.prompt.choice", choose)
    selected = select_model(
        "ready",
        [
            {"id": "ready", "provider": "one", "model": "small", "available": True},
            {"id": "offline", "provider": "two", "model": "big", "available": False},
        ],
    )
    assert selected == "ready"
    assert [value for value, _ in captured["options"]] == ["ready"]
    assert "offline" in str(captured["title"])
    assert "unavailable" in str(captured["title"])


def test_summary_cache_changes_with_profile_identity(tmp_path):
    from tests.test_context_retrieval import Summarizer

    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "cache.db", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repos)
            manager = ContextBudgetManager(
                sessions=repos.sessions,
                compactions=repos.compactions,
                settings=ContextSettings(),
                context_window=10000,
                max_output_tokens=2048,
                model_profile="test",
                model_name="test",
                tool_schemas=[],
            )
            older = [{"role": "user", "content": "keep this task"}]
            summarizer = Summarizer()
            manager.cache_identity = "provider1/profile/model1/adaptive"
            first = await manager.get_or_create_compaction(
                session_id=session.id,
                run_id=prepared.run.id,
                older=older,
                summarizer=summarizer,
            )
            calls = len(summarizer.requests)
            manager.cache_identity = "provider2/profile/model2/adaptive"
            second = await manager.get_or_create_compaction(
                session_id=session.id,
                run_id=prepared.run.id,
                older=older,
                summarizer=summarizer,
            )
            assert second.id != first.id
            assert len(summarizer.requests) > calls
        finally:
            await repos.close()

    asyncio.run(scenario())


def test_historical_stats_label_unknown_usage(tmp_path):
    from types import SimpleNamespace
    from capslock.cli.command_handlers.diagnostics import stats

    class UI:
        text = ""

        async def show(self, title, text):
            self.text = text

    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "stats.db", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repos)
            decision = await repos.models.record_decision(
                prepared.run.id,
                role="reasoning",
                candidates=[],
                selected="test",
                reasons={},
            )
            call = await repos.models.start_call(
                prepared.run.id,
                decision_id=decision,
                role="reasoning",
                profile="test",
                provider="test",
                model="test",
                attempt=1,
                data_policy="shared",
                fallback_from=None,
            )
            await repos.models.finish_call(call, duration_ms=1, usage_source="unknown")
            ui = UI()
            context = SimpleNamespace(
                application=SimpleNamespace(repositories=repos),
                session=SimpleNamespace(session_id=session.id),
                ui=ui,
            )
            await stats(context, ["/stats", "session"], "")
            assert "usage unknown" in ui.text
            assert "$0.000000" not in ui.text
        finally:
            await repos.close()

    asyncio.run(scenario())


def test_trace_counts_manual_action_wait_without_execution_or_auto(tmp_path):
    from capslock.domain import ActionType, ApprovalDecision
    from capslock.permissions import PermissionMode
    from capslock.application.action_system import FileActionHandler
    from capslock.policy import WorkspacePolicy
    from tests.test_actions import coordinator

    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "approval.db", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repos)

            async def approve(action):
                return ApprovalDecision.APPROVE

            manual = coordinator(
                repos,
                session.id,
                prepared.run.id,
                [FileActionHandler(WorkspacePolicy(tmp_path))],
                approval_authorizer=approve,
            )
            action = await manual.propose(
                ActionType.FILE_CREATE, path="manual.txt", content="manual"
            )
            automatic = coordinator(
                repos,
                session.id,
                prepared.run.id,
                [FileActionHandler(WorkspacePolicy(tmp_path))],
                mode=PermissionMode.FULL_ACCESS,
            )
            await automatic.propose(
                ActionType.FILE_CREATE, path="automatic.txt", content="automatic"
            )
            await repos.database.execute(
                "UPDATE actions SET created_at='2026-09-24T00:00:00+00:00', decided_at='2026-09-24T00:00:02+00:00', started_at='2026-09-24T00:00:02+00:00', finished_at='2026-09-24T00:01:00+00:00' WHERE id=?",
                (action.id,),
            )
            rows = await repos.performance.summary()
            waits = [
                row
                for row in rows
                if row["category"] == "approval" and row["name"] == "action_wait"
            ]
            assert len(waits) == 1
            assert waits[0]["samples"] == 1
            assert abs(waits[0]["total_ms"] - 2000) < 1
        finally:
            await repos.close()

    asyncio.run(scenario())
