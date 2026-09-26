"""Workspace recovery is exclusive; child readers explicitly share an owner."""

import asyncio
import sys

import pytest

from capslock.domain import RunStepKind
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import workflow_service


def test_second_owner_is_rejected_before_active_step_recovery(tmp_path):
    async def scenario():
        path = tmp_path / "state.sqlite3"
        first = await WorkspaceRepositories.open(path, workspace=tmp_path)
        second = None
        try:
            session = await first.sessions.create("model")
            prepared = await workflow_service(first).prepare(session.id, "question")
            step = await first.run_journal.create_step(
                prepared.run.id, RunStepKind.MODEL
            )
            with pytest.raises(RuntimeError, match="already open"):
                second = await WorkspaceRepositories.open(path, workspace=tmp_path)
            row = await first.database.fetch_one(
                "SELECT status FROM run_steps WHERE id=?", (step.id,)
            )
            assert row[0] == "running"
        finally:
            if second is not None:
                await second.close()
            await first.close()
        reopened = await WorkspaceRepositories.open(path, workspace=tmp_path)
        try:
            row = await reopened.database.fetch_one(
                "SELECT status FROM run_steps WHERE id=?", (step.id,)
            )
            assert row[0] == "cancelled"
        finally:
            await reopened.close()

    asyncio.run(scenario())


def test_shared_child_owner_preserves_recovery_and_holds_lease(tmp_path):
    async def scenario():
        path = tmp_path / "state.sqlite3"
        first = await WorkspaceRepositories.open(path, workspace=tmp_path)
        session = await first.sessions.create("model")
        prepared = await workflow_service(first).prepare(session.id, "question")
        step = await first.run_journal.create_step(prepared.run.id, RunStepKind.MODEL)
        child = None
        try:
            child = await WorkspaceRepositories.open(
                path, workspace=tmp_path, shared_owner=True
            )
            row = await child.database.fetch_one(
                "SELECT status FROM run_steps WHERE id=?", (step.id,)
            )
            assert row[0] == "running"
        finally:
            await first.close()
        assert child is not None
        try:
            with pytest.raises(RuntimeError, match="already open"):
                await WorkspaceRepositories.open(path, workspace=tmp_path)
        finally:
            await child.close()
        reopened = await WorkspaceRepositories.open(path, workspace=tmp_path)
        await reopened.close()

    asyncio.run(scenario())


def test_workspace_lease_is_enforced_across_processes(tmp_path):
    async def scenario():
        path = tmp_path / "state.sqlite3"
        first = await WorkspaceRepositories.open(path, workspace=tmp_path)
        try:
            code = """import asyncio,sys
from pathlib import Path
from capslock.storage.repositories import WorkspaceRepositories
async def main():
 try:
  repo = await WorkspaceRepositories.open(Path(sys.argv[1]),workspace=Path(sys.argv[2]))
 except RuntimeError as exc:
  print(str(exc));return
 await repo.close()
 raise AssertionError('second process acquired live workspace')
asyncio.run(main())
"""
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                str(path),
                str(tmp_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await process.communicate()
            assert process.returncode == 0, err.decode()
            assert "already open" in out.decode()
        finally:
            await first.close()

    asyncio.run(scenario())


def test_crashed_process_releases_workspace_lease(tmp_path):
    async def scenario():
        path = tmp_path / "state.sqlite3"
        code = """import asyncio,sys
from pathlib import Path
from capslock.storage.repositories import WorkspaceRepositories
async def main():
 repo = await WorkspaceRepositories.open(Path(sys.argv[1]),workspace=Path(sys.argv[2]))
 print('ready',flush=True)
 await asyncio.Event().wait()
asyncio.run(main())
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            str(path),
            str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert await asyncio.wait_for(process.stdout.readline(), 5) == b"ready\n"
            process.kill()
            await process.wait()
            repositories = await WorkspaceRepositories.open(path, workspace=tmp_path)
            await repositories.close()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(scenario())


def test_repository_startup_failure_releases_owner_lease(tmp_path, monkeypatch):
    from capslock.storage.repositories.journal.repository import RunJournalRepository

    async def scenario():
        path = tmp_path / "state.sqlite3"
        original = RunJournalRepository.interrupt_active

        async def fail(self):
            raise RuntimeError("injected recovery failure")

        monkeypatch.setattr(RunJournalRepository, "interrupt_active", fail)
        with pytest.raises(RuntimeError, match="injected"):
            await WorkspaceRepositories.open(path, workspace=tmp_path)
        monkeypatch.setattr(RunJournalRepository, "interrupt_active", original)
        repositories = await WorkspaceRepositories.open(path, workspace=tmp_path)
        await repositories.close()

    asyncio.run(scenario())


def test_cancelled_database_startup_closes_connection_and_releases_lease(
    tmp_path, monkeypatch
):
    from capslock.storage.async_database import WorkspaceDatabase

    async def scenario():
        entered = asyncio.Event()
        connections = []
        original = WorkspaceDatabase._configure

        async def block(self):
            connections.append(self.connection)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(WorkspaceDatabase, "_configure", block)
        task = asyncio.create_task(WorkspaceDatabase.open(tmp_path / "cancel.sqlite3"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            with pytest.raises(ValueError, match="closed|no active"):
                await connections[0].execute("SELECT 1")
        finally:
            await connections[0].close()
        monkeypatch.setattr(WorkspaceDatabase, "_configure", original)
        database = await WorkspaceDatabase.open(tmp_path / "cancel.sqlite3")
        await database.close()

    asyncio.run(scenario())
