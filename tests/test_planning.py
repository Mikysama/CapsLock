"""Plan Mode state, storage, and capability-boundary tests."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from capslock.cli.commands import CommandOutcomeKind
from capslock.cli.context import CliContext
from capslock.cli.command_ui import PlanApprovalResult
from capslock.cli.plans import plan_command
from capslock.domain import PlanStatus
from capslock.permissions import PermissionMode
from capslock.planning import PlanningService
from capslock.policy import WorkspacePolicy
from capslock.storage.repositories import WorkspaceRepositories
from capslock.tooling.contracts import (
    ExecutionContext,
    PlanToolVisibility,
    ResolvedToolPolicy,
    ToolOutcome,
    ToolPause,
    define_tool,
)
from capslock.tooling.executor import ToolRuntime
from capslock.tooling.planning import PlanningBoundaryMiddleware


def test_plan_revisions_are_hash_bound_and_implementation_is_idempotent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        service = PlanningService(repositories.plans, root=tmp_path / "plans")
        try:
            session = await repositories.sessions.create("test-model")
            plan, first = await service.create(
                session.id,
                "Add Plan Mode",
                entry_source="slash",
                base_permission_mode="full_access",
            )
            assert plan.status is PlanStatus.DRAFT
            assert service.path(plan).read_text(encoding="utf-8") == first.content

            plan, second = await service.update(
                session.id,
                "# Plan\n\nImplement a safe planning boundary.\n",
                expected_sha256=first.sha256,
                source="model",
                run_id=None,
            )
            assert second.ordinal == 2
            with pytest.raises(ValueError, match="changed"):
                await service.update(
                    session.id,
                    "# stale\n",
                    expected_sha256=first.sha256,
                    source="model",
                )

            child = await repositories.sessions.derive(
                session.id, title="Plan branch", derivation_kind="branch"
            )
            cloned = await service.clone_active(
                session.id,
                child.id,
                entry_source="branch",
                base_permission_mode="full_access",
            )
            assert cloned is not None
            assert cloned[0].parent_plan_id == plan.id
            assert cloned[1].content == second.content
            child_updated = await service.update(
                child.id,
                second.content + "\nChild-only detail.\n",
                expected_sha256=cloned[1].sha256,
                source="model",
            )
            assert child_updated[1].sha256 != second.sha256
            assert (await service.current(session.id))[1].sha256 == second.sha256

            request = await repositories.plans.submit(
                plan.id,
                expected_sha256=second.sha256,
                run_id=None,
                invocation_id=None,
            )
            await repositories.plans.decide(
                request.id,
                choice="implement",
                feedback=None,
                base_permission_mode="full_access",
            )
            first_implementation = await repositories.plans.ensure_implementation(
                request.id
            )
            second_implementation = await repositories.plans.ensure_implementation(
                request.id
            )
            assert first_implementation == second_implementation
            item = await repositories.work_items.require(
                first_implementation.work_item_id
            )
            assert second.sha256 in item.question
            assert "not a permission grant" in item.question
            assert (await repositories.plans.require(plan.id)).status is PlanStatus.IMPLEMENTING
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_plan_boundary_blocks_hidden_tools_even_in_full_access(tmp_path: Path) -> None:
    class ActivePlanning:
        async def is_active(self, session_id: str) -> bool:
            return True

    async def execute(context, arguments):
        return ToolOutcome.success(arguments)

    policy_calls = 0

    async def hidden_policy(arguments, context):
        nonlocal policy_calls
        policy_calls += 1
        return ResolvedToolPolicy(external_side_effects=True)

    hidden = define_tool(
        "shell",
        "Hidden side effect.",
        {"type": "object"},
        execute,
        policy=hidden_policy,
    )
    local = define_tool(
        "read_local",
        "Read local data.",
        {"type": "object"},
        execute,
        policy=ResolvedToolPolicy.safe_read(),
        plan_visibility=PlanToolVisibility.LOCAL_READ,
    )
    runtime = ToolRuntime(
        [hidden, local], middleware=(PlanningBoundaryMiddleware(),)
    )
    context = ExecutionContext(
        session_id="session",
        run_id="run",
        policy=WorkspacePolicy(tmp_path),
        event=lambda *args, **kwargs: None,
        actions=object(),
        permission_mode=PermissionMode.FULL_ACCESS,
        planning=ActivePlanning(),
    )
    denied = asyncio.run(runtime.invoke("shell", context, {})).outcome
    allowed = asyncio.run(runtime.invoke("read_local", context, {})).outcome
    assert denied.error_code == "plan_mode_read_only"
    assert not denied.executed
    assert policy_calls == 0
    assert allowed.ok and allowed.executed
    assert [schema["function"]["name"] for schema in runtime.plan_schemas] == [
        "read_local"
    ]


def test_plan_boundary_revalidates_a_resumed_hidden_invocation(tmp_path: Path) -> None:
    class ActivePlanning:
        async def is_active(self, session_id: str) -> bool:
            return True

    resumed = False
    policy_calls = 0

    async def execute(context, arguments):
        return ToolOutcome.success(arguments)

    async def resume(context, arguments, pause, response, reporter):
        nonlocal resumed
        del context, arguments, pause, response, reporter
        resumed = True
        return ToolOutcome.success({})

    async def policy(arguments, context):
        nonlocal policy_calls
        del arguments, context
        policy_calls += 1
        return ResolvedToolPolicy(external_side_effects=True)

    runtime = ToolRuntime(
        [
            define_tool(
                "stale_write",
                "A stale paused write.",
                {"type": "object"},
                execute,
                policy=policy,
                resume=resume,
            )
        ],
        middleware=(PlanningBoundaryMiddleware(),),
    )
    context = ExecutionContext(
        session_id="session",
        run_id="run",
        policy=WorkspacePolicy(tmp_path),
        event=lambda *args, **kwargs: None,
        actions=object(),
        permission_mode=PermissionMode.FULL_ACCESS,
        planning=ActivePlanning(),
    )
    result = asyncio.run(
        runtime.resume(
            "stale_write",
            context,
            {},
            ToolPause("approval", "request", {}, {}),
            {"choice": "approve"},
        )
    ).outcome
    assert result.error_code == "plan_mode_read_only"
    assert not result.executed
    assert policy_calls == 0
    assert not resumed


def test_plan_mirror_rejects_symlink(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        root = tmp_path / "plans"
        service = PlanningService(repositories.plans, root=root)
        try:
            session = await repositories.sessions.create("test-model")
            plan, revision = await service.create(
                session.id,
                "Symlink test",
                entry_source="slash",
                base_permission_mode="approve_for_me",
            )
            path = service.path(plan)
            path.unlink()
            path.symlink_to(tmp_path / "outside.md")
            with pytest.raises(ValueError, match="plan mirror"):
                await service.sync_mirror(plan, revision)
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_plan_slash_workflow_feedback_reject_resume_and_exit(tmp_path: Path) -> None:
    class UI:
        def __init__(self) -> None:
            self.selection: str | None = None
            self.feedback: str | None = None
            self.markdown: list[tuple[str, str]] = []

        async def select(self, title, choices):
            del title, choices
            return self.selection

        async def confirm(self, title, detail, *, default=False):
            del title, detail, default
            return True

        async def show_markdown(self, title, content):
            self.markdown.append((title, content))

        async def input_text(self, title, prompt):
            del title, prompt
            return self.feedback

        async def request_plan_entry(self, objective):
            del objective
            return True

        async def request_plan_approval(self, **kwargs):
            self.markdown.append(("Ready to code?", kwargs["content"]))
            return PlanApprovalResult(self.selection or "feedback", self.feedback)

    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "commands.sqlite3", workspace=tmp_path
        )
        planning = PlanningService(repositories.plans, root=tmp_path / "plans")
        session_record = await repositories.sessions.create("test-model")

        class Session:
            session_id = session_record.id
            permission_mode = PermissionMode.APPROVE_FOR_ME

            def __init__(self) -> None:
                self.planning = planning
                self.enqueued: list[str] = []

            async def enqueue(self, question):
                self.enqueued.append(question)
                return SimpleNamespace(id=f"work-{len(self.enqueued)}", question=question)

            async def decide_plan_request(self, identifier, choice, *, feedback=None):
                return await repositories.plans.decide(
                    identifier,
                    choice=choice,
                    feedback=feedback,
                    base_permission_mode=self.permission_mode.value,
                )

        session = Session()
        ui = UI()
        context = CliContext(
            Console(file=io.StringIO(), force_terminal=False), session, ui=ui
        )
        try:
            entered = await plan_command(
                context, ["/plan", "Design", "the", "change"], "/plan Design the change"
            )
            assert entered.kind is CommandOutcomeKind.ENQUEUE
            assert session.enqueued == ["Design the change"]

            await plan_command(context, ["/plan", "show"], "/plan show")
            assert ui.markdown[-1][1].startswith("# Plan")

            ui.selection = "feedback"
            ui.feedback = "Add rollback details"
            continued = await plan_command(
                context, ["/plan", "submit"], "/plan submit"
            )
            assert continued.kind is CommandOutcomeKind.ENQUEUE
            assert session.enqueued[-1] == "Add rollback details"
            active = await planning.current(session_record.id)
            assert active is not None and active[0].status is PlanStatus.DRAFT

            ui.selection = "reject"
            ui.feedback = None
            rejected = await plan_command(
                context, ["/plan", "submit"], "/plan submit"
            )
            assert rejected.kind is CommandOutcomeKind.HANDLED
            assert await planning.current(session_record.id) is None

            await plan_command(context, ["/plan"], "/plan")
            resumed = await planning.current(session_record.id)
            assert resumed is not None
            assert resumed[0].parent_plan_id is not None

            await plan_command(context, ["/plan", "exit"], "/plan exit")
            assert await planning.current(session_record.id) is None
        finally:
            await repositories.close()

    asyncio.run(scenario())
