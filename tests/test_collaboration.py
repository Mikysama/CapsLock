"""Child-agent collaboration tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from capslock.structured_output import child_agent_response_format

from capslock.collaboration import (
    AgentOutputVerifier,
    AgentTaskContract,
    AgentTaskState,
    AgentWorkspaceManager,
    CapabilityGrant,
    CapabilityKind,
    CollaborationService,
    MailboxMessageKind,
    VerificationError,
    VerificationRequirement,
)
from capslock.storage.repositories import WorkspaceRepositories
from capslock.tooling.contracts import ExecutionContext
from capslock.tooling.tools import workspace_tools
from capslock.tooling.tools.collaboration import _reserve_parent_budget
from capslock.domain import BudgetSnapshot, RunLimits, RunMode
from capslock.policy import WorkspacePolicy
from capslock.workspace_writes import WorkspaceMutationCoordinator
from tests.helpers import workspace_run


def test_child_tool_catalog_cannot_delegate_again() -> None:
    assert "delegate_agents" in workspace_tools().names
    assert "delegate_agents" not in workspace_tools(include_collaboration=False).names
    assert {
        "create_agent_team",
        "start_agent",
        "create_agent_task",
        "assign_agent_task",
        "follow_up_agent",
        "resume_agent",
        "get_agent_team",
        "stop_agent",
        "send_agent_message",
    }.issubset(workspace_tools().names)


def test_parent_budget_is_divided_before_child_launch(tmp_path: Path) -> None:
    class Governor:
        async def current(self):
            return BudgetSnapshot(
                RunMode.EXEC,
                "parent",
                RunLimits(
                    max_tool_rounds=10,
                    max_tool_calls=8,
                    max_duration_seconds=4,
                    max_tokens=100,
                    max_budget_usd=2,
                ),
                tool_rounds=2,
            )

    context = ExecutionContext(
        session_id="session",
        run_id="parent",
        policy=WorkspacePolicy(tmp_path),
        event=lambda *args, **kwargs: None,
        actions=object(),
        governor=Governor(),
    )
    contracts = [
        AgentTaskContract.create("parent", objective) for objective in ("a", "b")
    ]
    reserved = asyncio.run(_reserve_parent_budget(context, contracts))
    assert [item.limits["max_tool_rounds"] for item in reserved] == [3, 3]
    assert [item.limits["max_tokens"] for item in reserved] == [33, 33]
    assert [item.limits["max_budget_usd"] for item in reserved] == [2 / 3, 2 / 3]


def test_contract_defaults_to_no_mutating_capabilities() -> None:
    contract = AgentTaskContract.create("parent", "inspect code")
    assert contract.capabilities == ()
    assert contract.limits == {"max_tool_rounds": 16}
    assert contract.digest() == contract.digest()
    with pytest.raises(ValueError, match="plugin name"):
        CapabilityGrant(CapabilityKind.PLUGIN)
    with pytest.raises(ValueError, match="secret-like"):
        AgentTaskContract.create(
            "parent", "inspect code", input_context={"api_key": "secret"}
        )


def test_workspace_snapshot_excludes_private_state_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "readme.md").write_text("public", encoding="utf-8")
    (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (workspace / ".env.staging").write_text("TOKEN=secret", encoding="utf-8")
    (workspace / ".capslock").mkdir()
    (workspace / ".capslock" / "state").write_text("private", encoding="utf-8")
    manager = AgentWorkspaceManager(workspace, state_root=tmp_path / "agents")
    snapshot = manager.create("task")
    assert (snapshot.root / "readme.md").read_text(encoding="utf-8") == "public"
    assert not (snapshot.root / ".env").exists()
    assert not (snapshot.root / ".env.staging").exists()
    assert not (snapshot.root / ".capslock").exists()
    manager.cleanup(snapshot)

    (workspace / "linked").symlink_to(workspace / "readme.md")
    with pytest.raises(ValueError, match="symlink"):
        manager.create("linked-task")


def test_verified_artifact_is_published_without_overwriting_parent_change(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    report = workspace / "report.md"
    report.write_text("before", encoding="utf-8")
    manager = AgentWorkspaceManager(workspace, state_root=tmp_path / "agents")
    snapshot = manager.create("task")
    child_report = snapshot.root / "report.md"
    child_report.write_text("child", encoding="utf-8")
    artifact = {
        "path": "report.md",
        "sha256": hashlib.sha256(b"child").hexdigest(),
    }
    manager.publish_artifacts(snapshot, (artifact,), allowed_paths=("report.md",))
    assert report.read_text(encoding="utf-8") == "child"
    manager.cleanup(snapshot)

    report.write_text("before", encoding="utf-8")
    snapshot = manager.create("conflict")
    (snapshot.root / "report.md").write_text("child", encoding="utf-8")
    report.write_text("parent", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after child snapshot"):
        manager.publish_artifacts(snapshot, (artifact,), allowed_paths=("report.md",))


def test_artifact_publish_revalidates_after_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    report = workspace / "report.md"
    report.write_text("before", encoding="utf-8")
    manager = AgentWorkspaceManager(workspace, state_root=tmp_path / "agents")
    snapshot = manager.create("task")
    child = snapshot.root / "report.md"
    child.write_text("child", encoding="utf-8")
    original_copy = __import__("shutil").copy2

    def race(source, target, *args, **kwargs):
        if str(target).endswith(".capslock-agent"):
            report.write_text("parent", encoding="utf-8")
        return original_copy(source, target, *args, **kwargs)

    monkeypatch.setattr("capslock.collaboration.workspace.shutil.copy2", race)
    artifact = {"path": "report.md", "sha256": hashlib.sha256(b"child").hexdigest()}
    with pytest.raises(ValueError, match="changed after child snapshot"):
        manager.publish_artifacts(snapshot, (artifact,), allowed_paths=("report.md",))
    assert report.read_text(encoding="utf-8") == "parent"


def test_artifact_publish_rolls_back_partial_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    for name in ("one.md", "two.md"):
        (workspace / name).write_text(f"before-{name}", encoding="utf-8")
    manager = AgentWorkspaceManager(workspace, state_root=tmp_path / "agents")
    snapshot = manager.create("task")
    artifacts = []
    for name in ("one.md", "two.md"):
        content = f"child-{name}"
        (snapshot.root / name).write_text(content, encoding="utf-8")
        artifacts.append(
            {"path": name, "sha256": hashlib.sha256(content.encode()).hexdigest()}
        )

    original_replace = Path.replace
    replacements = 0

    def fail_second(source: Path, target: Path):
        nonlocal replacements
        if source.name.endswith(".capslock-agent"):
            replacements += 1
            if replacements == 2:
                raise OSError("simulated publish failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    with pytest.raises(OSError, match="simulated publish failure"):
        manager.publish_artifacts(
            snapshot, tuple(artifacts), allowed_paths=("one.md", "two.md")
        )
    assert (workspace / "one.md").read_text(encoding="utf-8") == "before-one.md"
    assert (workspace / "two.md").read_text(encoding="utf-8") == "before-two.md"


def test_artifact_publish_retains_backup_when_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    for name in ("one.md", "two.md"):
        (workspace / name).write_text(f"before-{name}", encoding="utf-8")
    manager = AgentWorkspaceManager(workspace, state_root=tmp_path / "agents")
    snapshot = manager.create("task")
    artifacts = []
    for name in ("one.md", "two.md"):
        content = f"child-{name}"
        (snapshot.root / name).write_text(content, encoding="utf-8")
        artifacts.append(
            {"path": name, "sha256": hashlib.sha256(content.encode()).hexdigest()}
        )

    original_replace = Path.replace
    replacements = 0

    def fail_publish_and_restore(source: Path, target: Path):
        nonlocal replacements
        if source.name.endswith(".capslock-agent"):
            replacements += 1
            if replacements == 2:
                raise OSError("simulated publish failure")
        if source.name.endswith(".capslock-backup"):
            raise OSError("simulated restore failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_publish_and_restore)
    with pytest.raises(OSError, match="rollback needs recovery") as error:
        manager.publish_artifacts(
            snapshot, tuple(artifacts), allowed_paths=("one.md", "two.md")
        )

    backups = list(workspace.glob("*.capslock-backup"))
    assert len(backups) == 1
    assert str(backups[0]) in str(error.value)
    assert backups[0].read_text(encoding="utf-8") == "before-one.md"


def test_artifact_publish_supports_mixed_create_and_update(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "existing.md").write_text("before", encoding="utf-8")
    manager = AgentWorkspaceManager(workspace, state_root=tmp_path / "agents")
    snapshot = manager.create("task")
    (snapshot.root / "existing.md").write_text("updated", encoding="utf-8")
    created = snapshot.root / "reports" / "new.md"
    created.parent.mkdir()
    created.write_text("created", encoding="utf-8")
    artifacts = tuple(
        {
            "path": path,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }
        for path, content in (
            ("existing.md", "updated"),
            ("reports/new.md", "created"),
        )
    )

    manager.publish_artifacts(
        snapshot,
        artifacts,
        allowed_paths=("existing.md", "reports"),
    )

    assert (workspace / "existing.md").read_text(encoding="utf-8") == "updated"
    assert (workspace / "reports" / "new.md").read_text(encoding="utf-8") == "created"
    assert not list(workspace.rglob("*.capslock-agent"))
    assert not list(workspace.rglob("*.capslock-backup"))


def test_artifact_publish_serializes_coordinated_workspace_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    report = workspace / "report.md"
    report.write_text("before", encoding="utf-8")
    coordinator = WorkspaceMutationCoordinator()
    manager = AgentWorkspaceManager(
        workspace,
        state_root=tmp_path / "agents",
        write_coordinator=coordinator,
    )
    snapshot = manager.create("task")
    (snapshot.root / "report.md").write_text("child", encoding="utf-8")
    artifact = {"path": "report.md", "sha256": hashlib.sha256(b"child").hexdigest()}
    entered = threading.Event()
    release = threading.Event()
    writer_finished = threading.Event()
    failures = []
    original_revalidate = manager._revalidate_parent_artifacts

    def pause_while_locked(staged):
        entered.set()
        if not release.wait(2):
            raise TimeoutError("test did not release artifact publisher")
        original_revalidate(staged)

    monkeypatch.setattr(manager, "_revalidate_parent_artifacts", pause_while_locked)

    def publish() -> None:
        try:
            manager.publish_artifacts(
                snapshot, (artifact,), allowed_paths=("report.md",)
            )
        except BaseException as exc:
            failures.append(exc)

    def coordinated_writer() -> None:
        with coordinator.lock(workspace):
            report.write_text("writer", encoding="utf-8")
        writer_finished.set()

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert entered.wait(2)
    writer = threading.Thread(target=coordinated_writer)
    writer.start()
    assert not writer_finished.wait(0.05)
    release.set()
    publisher.join(2)
    writer.join(2)

    assert not publisher.is_alive() and not writer.is_alive()
    assert failures == []
    assert writer_finished.is_set()
    assert report.read_text(encoding="utf-8") == "writer"


def test_output_verifier_checks_allowlist_and_digest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    artifact = child / "reports" / "result.md"
    artifact.parent.mkdir()
    artifact.write_text("verified", encoding="utf-8")
    contract = AgentTaskContract.create(
        "parent",
        "write report",
        allowed_paths=("reports",),
        verification_requirements=VerificationRequirement(
            required_paths=("reports/result.md",), required_checks=("pytest",)
        ),
    )
    verifier = AgentOutputVerifier()
    from capslock.collaboration import WorkspaceSnapshot

    output = verifier.verify(
        contract,
        WorkspaceSnapshot(source, child),
        {
            "summary": "done",
            "evidence": [{"path": "reports/result.md", "sha256": None}],
            "artifacts": [
                {
                    "path": "reports/result.md",
                    "sha256": hashlib.sha256(b"verified").hexdigest(),
                }
            ],
            "checks": [{"name": "pytest", "status": "passed"}],
            "memory_proposals": None,
        },
    )
    assert output.verified is True
    assert output.evidence[0]["sha256"] == hashlib.sha256(b"verified").hexdigest()
    assert output.artifacts[0]["bytes"] == 8
    with pytest.raises(VerificationError, match="digest mismatch"):
        verifier.verify(
            contract,
            WorkspaceSnapshot(source, child),
            {
                "summary": "done",
                "evidence": [],
                "artifacts": [{"path": "reports/result.md", "sha256": "0" * 64}],
                "checks": [{"name": "pytest", "status": "passed"}],
                "memory_proposals": None,
            },
        )


def test_child_agent_output_schema_is_provider_strict() -> None:
    response_format = child_agent_response_format(
        {
            "type": "object",
            "properties": {"result_code": {"type": "string"}},
            "required": ["result_code"],
            "additionalProperties": False,
        }
    )
    definition = response_format["json_schema"]
    schema = definition["schema"]
    assert definition["strict"] is True
    assert definition["name"] == "child_agent_result"
    assert {"summary", "evidence", "artifacts", "checks", "result_code"} <= set(
        schema["required"]
    )
    assert schema["additionalProperties"] is False


def test_bounded_scheduler_preserves_contract_order_and_isolates_failure(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        (workspace / "input.txt").write_text("input", encoding="utf-8")
        repositories = await WorkspaceRepositories.open(
            workspace / ".capslock" / "state" / "capslock.sqlite3",
            workspace=workspace,
        )
        try:
            _session, prepared = await workspace_run(repositories)
            active = 0
            maximum = 0
            paired = asyncio.Event()

            async def runner(contract, _snapshot):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                try:
                    if active == 2:
                        paired.set()
                    if contract.objective in {"first", "bad"}:
                        await asyncio.wait_for(paired.wait(), timeout=1)
                    if contract.objective == "bad":
                        raise RuntimeError("isolated failure")
                    return {
                        "summary": contract.objective,
                        "evidence": [],
                        "artifacts": [],
                        "checks": [],
                        "memory_proposals": None,
                    }
                finally:
                    active -= 1

            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(workspace),
                repository=repositories.collaboration,
                max_children=4,
                max_concurrency=2,
                child_runner=runner,
            )
            contracts = [
                AgentTaskContract.create(prepared.run.id, name)
                for name in ("first", "bad", "third")
            ]
            outputs = await service.delegate(contracts)
            assert [item.task_id for item in outputs] == [
                item.task_id for item in contracts
            ]
            assert [item.verified for item in outputs] == [True, False, True]
            assert maximum == 2
            assert await repositories.collaboration.active_tasks(prepared.run.id) == []
            statuses = [
                status
                async for status in service.stream_status(
                    [item.task_id for item in contracts]
                )
            ]
            assert statuses[-1]["tasks"] == {
                item.task_id: "completed" if item.objective != "bad" else "failed"
                for item in contracts
            }
            assert (await service.wait(contracts[0].task_id)).verified is True
            with pytest.raises(ValueError, match="isolated failure"):
                await service.validated_output(contracts[1].task_id)
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_cancel_queued_child_does_not_cancel_siblings(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        repositories = await WorkspaceRepositories.open(
            workspace / ".capslock" / "state" / "capslock.sqlite3",
            workspace=workspace,
        )
        try:
            _session, prepared = await workspace_run(repositories)
            release = asyncio.Event()

            async def runner(contract, _snapshot):
                if contract.objective == "first":
                    await release.wait()
                return {
                    "summary": contract.objective,
                    "evidence": [],
                    "artifacts": [],
                    "checks": [],
                    "memory_proposals": None,
                }

            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(workspace),
                repository=repositories.collaboration,
                max_concurrency=1,
                child_runner=runner,
            )
            contracts = [
                AgentTaskContract.create(prepared.run.id, objective)
                for objective in ("first", "cancelled", "third")
            ]
            delegated = asyncio.create_task(service.delegate(contracts))
            while contracts[1].task_id not in service._tasks:
                await asyncio.sleep(0)
            await service.cancel(contracts[1].task_id)
            release.set()
            outputs = await delegated
            assert [item.state for item in outputs] == [
                AgentTaskState.COMPLETED,
                AgentTaskState.CANCELLED,
                AgentTaskState.COMPLETED,
            ]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_fresh_workspace_contains_collaboration_tables(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            version = await repositories.database.fetch_one("PRAGMA user_version")
            assert int(version[0]) == 21
            tables = await repositories.database.fetch_all(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_%'"
            )
            assert {str(row[0]) for row in tables} == {
                "agent_tasks",
                "agent_workspaces",
                "agent_messages",
                "agent_mailbox",
                "agent_outputs",
                "agent_teams",
                "agent_workers",
                "agent_task_dependencies",
                "agent_attempts",
                "agent_checkpoints",
                "agent_budget_ledger",
                "agent_approval_links",
            }
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_non_current_collaboration_schema_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "state.sqlite3"
        repositories = await WorkspaceRepositories.open(path, workspace=tmp_path)
        await repositories.close()
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys=OFF")
        for table in (
            "agent_outputs",
            "agent_messages",
            "agent_workspaces",
            "agent_tasks",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=4")
        connection.commit()
        connection.close()
        with pytest.raises(Exception, match="schema is not supported"):
            await WorkspaceRepositories.open(path, workspace=tmp_path)

    asyncio.run(scenario())


def test_reopen_interrupts_active_child_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "state.sqlite3"
        repositories = await WorkspaceRepositories.open(path, workspace=tmp_path)
        _session, prepared = await workspace_run(repositories)
        contract = AgentTaskContract.create(prepared.run.id, "unfinished")
        await repositories.collaboration.create_task(contract)
        await repositories.collaboration.set_state(
            contract.task_id, AgentTaskState.RUNNING
        )
        await repositories.close()
        repositories = await WorkspaceRepositories.open(path, workspace=tmp_path)
        try:
            task = await repositories.collaboration.get_task(contract.task_id)
            assert task is not None
            assert task["state"] == "interrupted"
            with pytest.raises(ValueError, match="invalid child task transition"):
                await repositories.collaboration.set_state(
                    contract.task_id, AgentTaskState.CANCELLED
                )
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_concurrency_limit_is_shared_across_delegate_calls(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        repositories = await WorkspaceRepositories.open(
            workspace / ".capslock" / "state" / "capslock.sqlite3",
            workspace=workspace,
        )
        try:
            _session, prepared = await workspace_run(repositories)
            active = 0
            maximum = 0
            first_started = asyncio.Event()
            release = asyncio.Event()

            async def runner(contract, _snapshot):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                if contract.objective == "first":
                    first_started.set()
                    await release.wait()
                active -= 1
                return {
                    "summary": contract.objective,
                    "evidence": [],
                    "artifacts": [],
                    "checks": [],
                    "memory_proposals": None,
                }

            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(workspace),
                repository=repositories.collaboration,
                max_concurrency=1,
                child_runner=runner,
            )
            first = AgentTaskContract.create(prepared.run.id, "first")
            second = AgentTaskContract.create(prepared.run.id, "second")
            await service.delegate([first], background=True)
            await first_started.wait()
            await service.delegate([second], background=True)
            await asyncio.sleep(0.05)
            assert maximum == 1
            release.set()
            assert (await service.wait(first.task_id)).verified
            assert (await service.wait(second.task_id)).verified
            assert maximum == 1
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_background_agent_usage_is_added_to_parent_session_statistics(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        repositories = await WorkspaceRepositories.open(
            workspace / ".capslock" / "state" / "capslock.sqlite3",
            workspace=workspace,
        )
        try:
            _session, prepared = await workspace_run(repositories)

            async def runner(_contract, _snapshot):
                return {
                    "summary": "done",
                    "evidence": [],
                    "artifacts": [],
                    "checks": [],
                    "memory_proposals": None,
                    "_usage": {
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "cost_usd": 0.25,
                    },
                }

            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(workspace),
                repository=repositories.collaboration,
                child_runner=runner,
            )
            contract = AgentTaskContract.create(prepared.run.id, "background usage")
            await service.delegate([contract], background=True)
            assert (await service.wait(contract.task_id)).verified
            run = await repositories.database.fetch_one(
                "SELECT input_tokens,output_tokens,cost_usd FROM runs WHERE id=?",
                (prepared.run.id,),
            )
            assert run is not None
            assert tuple(run) == (7, 3, 0.25)
            ledger = await repositories.database.fetch_all(
                """SELECT operation FROM agent_budget_ledger
                   WHERE task_id=? ORDER BY created_at""",
                (contract.task_id,),
            )
            assert [row["operation"] for row in ledger] == ["reserve", "settle"]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_session_owner_can_control_background_task_across_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        repositories = await WorkspaceRepositories.open(
            workspace / ".capslock" / "state" / "capslock.sqlite3",
            workspace=workspace,
        )
        release = asyncio.Event()
        try:
            session, prepared = await workspace_run(repositories)

            async def runner(_contract, _snapshot):
                await release.wait()
                return {
                    "summary": "done",
                    "evidence": [],
                    "artifacts": [],
                    "checks": [],
                    "memory_proposals": None,
                }

            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(workspace),
                repository=repositories.collaboration,
                child_runner=runner,
            )
            contract = AgentTaskContract.create(prepared.run.id, "background")
            await service.delegate([contract], background=True)
            message = await service.send_message(
                contract.task_id,
                session_id=session.id,
                kind=MailboxMessageKind.INSTRUCTION,
                payload={"text": "continue"},
            )
            assert message["task_id"] == contract.task_id
            with pytest.raises(ValueError, match="controller"):
                await service.send_message(
                    contract.task_id,
                    session_id="another-session",
                    kind=MailboxMessageKind.INSTRUCTION,
                    payload={"text": "forbidden"},
                )
            release.set()
            await service.wait(contract.task_id)
        finally:
            release.set()
            await repositories.close()

    asyncio.run(scenario())


def test_agent_dag_claim_is_atomic_and_dependency_aware(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        repositories = await WorkspaceRepositories.open(
            workspace / ".capslock" / "state" / "capslock.sqlite3",
            workspace=workspace,
        )
        try:
            session, prepared = await workspace_run(repositories)
            team = await repositories.collaboration.create_team(
                session.id, "review", created_by_run_id=prepared.run.id
            )
            first_worker = await repositories.collaboration.create_worker(
                str(team["id"]), "one"
            )
            second_worker = await repositories.collaboration.create_worker(
                str(team["id"]), "two"
            )
            blocker = AgentTaskContract.create(prepared.run.id, "blocker")
            dependent = AgentTaskContract.create(prepared.run.id, "dependent")
            await repositories.collaboration.create_task(
                blocker, team_id=str(team["id"])
            )
            await repositories.collaboration.create_task(
                dependent,
                team_id=str(team["id"]),
                depends_on=(blocker.task_id,),
            )
            with pytest.raises(ValueError, match="dependencies"):
                await repositories.collaboration.claim_task(
                    dependent.task_id, str(first_worker["id"])
                )

            results = await asyncio.gather(
                repositories.collaboration.claim_task(
                    blocker.task_id, str(first_worker["id"])
                ),
                repositories.collaboration.claim_task(
                    blocker.task_id, str(second_worker["id"])
                ),
                return_exceptions=True,
            )
            assert sum(isinstance(item, dict) for item in results) == 1
            claim = next(item for item in results if isinstance(item, dict))
            await repositories.collaboration.start_attempt(str(claim["attempt_id"]))
            await repositories.collaboration.finish_attempt(
                str(claim["attempt_id"]), "completed"
            )
            assert await repositories.collaboration.claim_task(
                dependent.task_id, str(second_worker["id"])
            )
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_named_agent_profile_cannot_be_expanded_by_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        repositories = await WorkspaceRepositories.open(
            workspace / ".capslock" / "state" / "capslock.sqlite3",
            workspace=workspace,
        )
        try:
            session, prepared = await workspace_run(repositories)
            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(workspace),
                repository=repositories.collaboration,
                child_runner=lambda *_args: None,
            )
            team = await service.create_team(
                session.id, "safe", created_by_run_id=prepared.run.id
            )
            worker = await service.start_agent(
                session_id=session.id,
                team_id=str(team["id"]),
                name="reader",
                profile={"capabilities": [], "allowed_paths": []},
            )
            contract = AgentTaskContract.create(
                prepared.run.id,
                "try expansion",
                capabilities=(CapabilityGrant(CapabilityKind.COMMAND),),
            )
            await service.create_agent_task(
                contract, session_id=session.id, team_id=str(team["id"])
            )
            with pytest.raises(ValueError, match="exceeds"):
                await service.assign_agent_task(
                    contract.task_id, str(worker["id"]), session_id=session.id
                )
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_interrupted_agent_resumes_same_attempt_and_child_session(
    tmp_path: Path,
) -> None:
    class RecoveringRunner:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.resumed: list[str] = []

        async def __call__(self, _contract, _snapshot):
            raise AssertionError("restart recovery must not start a fresh child")

        async def resume_interrupted(self, contract, _snapshot):
            self.resumed.append(contract.task_id)
            await self.release.wait()
            return {
                "summary": "resumed",
                "evidence": [],
                "artifacts": [],
                "checks": [],
                "memory_proposals": None,
                "_child_run_id": "child-run-2",
            }

    async def scenario() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        repositories = await WorkspaceRepositories.open(
            workspace / ".capslock" / "state" / "capslock.sqlite3",
            workspace=workspace,
        )
        try:
            session, prepared = await workspace_run(repositories)
            runner = RecoveringRunner()
            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(workspace),
                repository=repositories.collaboration,
                child_runner=runner,
            )
            team = await service.create_team(
                session.id, "recovery", created_by_run_id=prepared.run.id
            )
            worker = await service.start_agent(
                session_id=session.id,
                team_id=str(team["id"]),
                name="persistent",
                profile={"capabilities": [], "allowed_paths": []},
            )
            contract = AgentTaskContract.create(prepared.run.id, "resume me")
            await service.create_agent_task(
                contract,
                session_id=session.id,
                team_id=str(team["id"]),
                worker_id=str(worker["id"]),
            )
            snapshot = service.workspace_manager.create(str(worker["id"]))
            await repositories.collaboration.attach_workspace(
                contract.task_id,
                worker_id=str(worker["id"]),
                mode="snapshot",
                path=str(snapshot.root),
                source_path=str(snapshot.source),
                baseline=service.workspace_manager.baseline(snapshot),
            )
            claim = await repositories.collaboration.claim_task(
                contract.task_id,
                str(worker["id"]),
                reservation=dict(contract.limits),
            )
            attempt_id = str(claim["attempt_id"])
            await repositories.collaboration.start_attempt(attempt_id)
            await repositories.collaboration.checkpoint_attempt(
                attempt_id,
                contract_sha256="wrong-digest",
                child_session_id="child-session-1",
                child_run_id="child-run-1",
                checkpoint={"cursor": 1},
                resumable=True,
            )
            await repositories.collaboration.interrupt_active()

            with pytest.raises(ValueError, match="checkpoint is not resumable"):
                await service.resume_agent(
                    str(worker["id"]),
                    session_id=session.id,
                    task_id=contract.task_id,
                )

            await repositories.collaboration.checkpoint_attempt(
                attempt_id,
                contract_sha256=contract.digest(),
                child_session_id="child-session-1",
                child_run_id="child-run-1",
                checkpoint={"cursor": 1},
                resumable=True,
            )
            await service.resume_agent(
                str(worker["id"]),
                session_id=session.id,
                task_id=contract.task_id,
            )
            while not runner.resumed:
                await asyncio.sleep(0)
            with pytest.raises(ValueError, match="not interrupted"):
                await service.resume_agent(
                    str(worker["id"]),
                    session_id=session.id,
                    task_id=contract.task_id,
                )
            runner.release.set()
            output = await service.wait(contract.task_id)
            assert output.verified is True
            assert runner.resumed == [contract.task_id]
            latest = await repositories.collaboration.latest_attempt(contract.task_id)
            assert latest is not None
            assert latest["id"] == attempt_id
            assert latest["state"] == "completed"
            task = await repositories.collaboration.get_task(contract.task_id)
            assert task is not None
            assert task["attempt_count"] == 1
            stored_worker = await repositories.collaboration.worker(str(worker["id"]))
            assert stored_worker is not None
            assert stored_worker["child_session_id"] == "child-session-1"
        finally:
            runner.release.set()
            await repositories.close()

    asyncio.run(scenario())


def test_persistent_worker_persists_artifact_baseline_between_followups(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        report = workspace / "report.md"
        report.write_text("initial", encoding="utf-8")
        repositories = await WorkspaceRepositories.open(
            workspace / ".capslock" / "state" / "capslock.sqlite3",
            workspace=workspace,
        )
        counter = 0
        try:
            session, prepared = await workspace_run(repositories)

            async def runner(_contract, snapshot):
                nonlocal counter
                counter += 1
                content = f"revision-{counter}"
                (snapshot.root / "report.md").write_text(content, encoding="utf-8")
                return {
                    "summary": content,
                    "evidence": [],
                    "artifacts": [
                        {
                            "path": "report.md",
                            "sha256": hashlib.sha256(content.encode()).hexdigest(),
                        }
                    ],
                    "checks": [],
                    "memory_proposals": None,
                }

            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(workspace),
                repository=repositories.collaboration,
                child_runner=runner,
            )
            team = await service.create_team(
                session.id, "writers", created_by_run_id=prepared.run.id
            )
            worker = await service.start_agent(
                session_id=session.id,
                team_id=str(team["id"]),
                name="writer",
                profile={"capabilities": [], "allowed_paths": ["report.md"]},
            )
            first = AgentTaskContract.create(
                prepared.run.id, "first", allowed_paths=("report.md",)
            )
            await service.create_agent_task(
                first,
                session_id=session.id,
                team_id=str(team["id"]),
                worker_id=str(worker["id"]),
            )
            await service.assign_agent_task(
                first.task_id, str(worker["id"]), session_id=session.id
            )
            assert (await service.wait(first.task_id)).verified
            assert report.read_text(encoding="utf-8") == "revision-1"

            second = AgentTaskContract.create(
                prepared.run.id, "second", allowed_paths=("report.md",)
            )
            await service.follow_up_agent(
                str(worker["id"]), second, session_id=session.id
            )
            assert (await service.wait(second.task_id)).verified
            assert report.read_text(encoding="utf-8") == "revision-2"
            rows = await repositories.database.fetch_all(
                "SELECT baseline_json FROM agent_workspaces WHERE worker_id=?",
                (str(worker["id"]),),
            )
            expected = hashlib.sha256(b"revision-2").hexdigest()
            assert rows
            assert all(json.loads(str(row[0]))["report.md"] == expected for row in rows)
        finally:
            await repositories.close()

    asyncio.run(scenario())
