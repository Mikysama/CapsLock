"""Child-agent collaboration tests."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest

from capslock.collaboration import (
    AgentOutputVerifier,
    AgentTaskContract,
    AgentTaskState,
    AgentWorkspaceManager,
    CapabilityGrant,
    CapabilityKind,
    CollaborationService,
    VerificationError,
    VerificationRequirement,
)
from capslock.storage.repositories import WorkspaceRepositories
from capslock.tooling.contracts import ExecutionContext
from capslock.tooling.tools import workspace_tools
from capslock.tooling.tools.collaboration import _reserve_parent_budget
from capslock.domain import BudgetSnapshot, RunLimits, RunMode
from capslock.policy import WorkspacePolicy
from tests.helpers import workspace_run


def test_child_tool_catalog_cannot_delegate_again() -> None:
    assert "delegate_agents" in workspace_tools().names
    assert "delegate_agents" not in workspace_tools(include_collaboration=False).names


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
            "evidence": [{"path": "reports/result.md"}],
            "artifacts": [
                {
                    "path": "reports/result.md",
                    "sha256": hashlib.sha256(b"verified").hexdigest(),
                }
            ],
            "checks": [{"name": "pytest", "status": "passed"}],
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
                "artifacts": [{"path": "reports/result.md", "sha256": "0" * 64}],
                "checks": [{"name": "pytest", "status": "passed"}],
            },
        )


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
                    return {"summary": contract.objective}
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
                return {"summary": contract.objective}

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
            assert int(version[0]) == 8
            tables = await repositories.database.fetch_all(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_%'"
            )
            assert {str(row[0]) for row in tables} == {
                "agent_tasks",
                "agent_workspaces",
                "agent_capabilities",
                "agent_messages",
                "agent_outputs",
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
            "agent_capabilities",
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
