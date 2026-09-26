"""Search backend and trusted process polling regression coverage."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.domain import (
    BudgetSnapshot,
    LoopDetectionSettings,
    RunLimits,
    RunMode,
    RunStopped,
)
from capslock.policy import WorkspacePolicy
from capslock.runtime.governance import RunGovernor
from capslock.shell import SandboxedCommand, SessionProcessManager
from capslock.tooling.contracts import ExecutionContext
from capslock.tooling.tools.filesystem.search import search_files
from capslock.tooling.tools.shell import process_output


def context(root: Path, **values):
    return ExecutionContext(
        session_id="session",
        run_id="run",
        policy=WorkspacePolicy(root),
        event=lambda *a, **kw: None,
        actions=object(),
        **values,
    )


def metadata(outcome):
    return json.loads(outcome.content[0].value)


def test_search_missing_backend_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "capslock.tooling.tools.filesystem.search.shutil.which", lambda _: None
    )
    result = asyncio.run(
        search_files(context(tmp_path), {"path": ".", "query": "needle"})
    )
    assert not result.ok and result.error_code == "search_backend_unavailable"


def test_search_invalid_regex_is_failure(tmp_path):
    (tmp_path / "one.txt").write_text("Alpha\nfoo bar\n中文\n")
    result = asyncio.run(search_files(context(tmp_path), {"path": ".", "query": "["}))
    assert not result.ok and result.error_code == "search_failed"


@pytest.mark.parametrize(
    "options, expected",
    [
        ({"query": "alpha"}, 0),
        ({"query": "alpha", "case_sensitive": False}, 1),
        ({"query": "foo|bar", "mode": "literal"}, 0),
        ({"query": "foo|bar", "mode": "regex"}, 1),
        ({"query": "中文"}, 1),
    ],
)
def test_search_explicit_semantics(tmp_path, options, expected):
    (tmp_path / "one.txt").write_text("Alpha\nfoo bar\n中文\n")
    result = asyncio.run(search_files(context(tmp_path), {"path": ".", **options}))
    assert result.ok and len(result.data) == expected
    assert metadata(result)["backend"] == "ripgrep"


def test_search_ignores_user_config_and_reports_truncation(tmp_path, monkeypatch):
    config = tmp_path / "rg.conf"
    config.write_text("--ignore-case\n")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    (tmp_path / "one.txt").write_text("Alpha\nneedle\nneedle\n")
    result = asyncio.run(
        search_files(context(tmp_path), {"path": ".", "query": "alpha"})
    )
    assert result.ok and result.data == []
    result = asyncio.run(
        search_files(context(tmp_path), {"path": ".", "query": "needle", "limit": 1})
    )
    assert len(result.data) == len(result.citations) == 1
    assert metadata(result)["truncated"] is True
    assert metadata(result)["stop_reason"] == "result_limit"


def test_search_hidden_and_privacy(tmp_path):
    (tmp_path / ".hidden.txt").write_text("needle")
    (tmp_path / ".env").write_text("needle secret")
    default = asyncio.run(
        search_files(context(tmp_path), {"path": ".", "query": "needle"})
    )
    hidden = asyncio.run(
        search_files(
            context(tmp_path), {"path": ".", "query": "needle", "include_hidden": True}
        )
    )
    assert default.data == []
    assert len(hidden.data) == 1 and hidden.data[0]["path"].endswith(".hidden.txt")


def test_search_releases_process_with_large_unread_output(tmp_path, monkeypatch):
    async def scenario():
        (tmp_path / "one.txt").write_text("needle\n")
        original = asyncio.create_subprocess_exec
        record = json.dumps(
            {
                "type": "match",
                "data": {"path": {"text": str(tmp_path / "one.txt")}, "line_number": 1},
            }
        )
        spawned = []

        async def spawn(*args, **kwargs):
            process = await original(
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write(({record!r} + '\\n') * 100000); sys.stdout.flush()",
                **kwargs,
            )
            spawned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        async with asyncio.timeout(4):
            result = await search_files(
                context(tmp_path), {"path": ".", "query": "needle", "limit": 1}
            )
        assert result.ok and metadata(result)["truncated"]
        assert spawned[0].returncode is not None

    asyncio.run(scenario())


def test_search_cancel_cleans_child_process(tmp_path, monkeypatch):
    async def scenario():
        original = asyncio.create_subprocess_exec
        spawned = []
        ready = asyncio.Event()

        async def spawn(*args, **kwargs):
            process = await original(
                sys.executable, "-c", "import time; time.sleep(10)", **kwargs
            )
            spawned.append(process)
            ready.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(
            search_files(context(tmp_path), {"path": ".", "query": "needle"})
        )
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawned[0].returncode is not None

    asyncio.run(scenario())


def test_search_reads_each_matched_file_once(tmp_path, monkeypatch):
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    (tmp_path / "one.txt").write_text("needle\nneedle\nneedle\n")
    monkeypatch.setattr(Path, "read_text", read)
    result = asyncio.run(
        search_files(context(tmp_path), {"path": ".", "query": "needle"})
    )
    assert result.ok and len(result.data) == 3
    assert reads.count(tmp_path / "one.txt") == 1


def test_process_offsets_and_capture_progress_after_truncation(tmp_path):
    async def scenario():
        temporary = tmp_path / "temp"
        temporary.mkdir()
        manager = SessionProcessManager(output_limit=4)
        job = await manager.start(
            "session",
            SandboxedCommand(
                (
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('abcdefgh'); sys.stderr.write('xyz')",
                ),
                tmp_path,
                temporary,
            ),
        )
        try:
            await asyncio.gather(*job.tasks)
            result = await process_output(
                context(tmp_path, process_manager=manager),
                {
                    "process_id": job.id,
                    "stdout_offset": 2,
                    "stderr_offset": 1,
                    "wait_ms": 0,
                },
            )
            assert result.data["stdout"] == "cd" and result.data["stderr"] == "yz"
            assert result.data["stdout_offset"] == 8
            assert result.data["stderr_offset"] == 3
            assert result.data["truncated"] is True
            assert job.progress_bytes == 11
            again = await process_output(
                context(tmp_path, process_manager=manager),
                {
                    "process_id": job.id,
                    "stdout_offset": 8,
                    "stderr_offset": 3,
                    "wait_ms": 0,
                },
            )
            assert again.data["stdout"] == again.data["stderr"] == ""
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_process_poll_wait_is_limited_by_run_deadline(tmp_path):
    async def scenario():
        temporary = tmp_path / "temp"
        temporary.mkdir()
        manager = SessionProcessManager()
        job = await manager.start(
            "session",
            SandboxedCommand(
                (sys.executable, "-c", "import time; time.sleep(10)"),
                tmp_path,
                temporary,
            ),
        )
        try:
            governor = SimpleNamespace(remaining_seconds=lambda: 0.02)
            started = time.monotonic()
            result = await process_output(
                context(tmp_path, process_manager=manager, governor=governor),
                {"process_id": job.id, "wait_ms": 30000},
            )
            elapsed = time.monotonic() - started
            assert result.ok and 0.01 <= elapsed < 0.5
        finally:
            await manager.close()

    asyncio.run(scenario())


class Governance:
    async def save(self, *args):
        pass

    async def reserve_attempt(self, *args, **kwargs):
        self.count = getattr(self, "count", 0) + 1
        return self.count

    async def finish_attempt(self, *args, **kwargs):
        pass

    async def usage(self, *args):
        return 0, 0, 0


def test_governor_running_poll_bypasses_repeats_but_stops_stalled_job():
    async def scenario():
        store = Governance()
        governor = RunGovernor(
            store,
            store,
            "run",
            BudgetSnapshot(RunMode.EXEC, "run", RunLimits()),
            [],
            LoopDetectionSettings(),
        )
        for _ in range(5):
            attempt, _, _ = await governor.before_tool(
                "process_output",
                {"process_id": "p"},
                trusted_poll={
                    "status": "running",
                    "last_progress_at": time.monotonic(),
                },
            )
            await governor.finish_tool(attempt, ok=True, duration_ms=0)
        with pytest.raises(RunStopped) as caught:
            await governor.before_tool(
                "process_output",
                {"process_id": "p"},
                trusted_poll={
                    "status": "running",
                    "last_progress_at": time.monotonic() - 121,
                },
            )
        assert caught.value.detail["pattern"] == "process_stalled"

    asyncio.run(scenario())


def test_governor_allows_completion_read_after_running_polls():
    async def scenario():
        store = Governance()
        governor = RunGovernor(
            store,
            store,
            "run",
            BudgetSnapshot(RunMode.EXEC, "run", RunLimits()),
            [],
            LoopDetectionSettings(),
        )
        for _ in range(5):
            attempt, _, _ = await governor.before_tool(
                "process_output",
                {"process_id": "p"},
                trusted_poll={
                    "status": "running",
                    "last_progress_at": time.monotonic(),
                },
            )
            await governor.finish_tool(attempt, ok=True, duration_ms=0)
        for _ in range(2):
            attempt, _, _ = await governor.before_tool(
                "process_output",
                {"process_id": "p"},
                trusted_poll={
                    "status": "completed",
                    "last_progress_at": time.monotonic(),
                },
            )
            await governor.finish_tool(attempt, ok=True, duration_ms=0)
        with pytest.raises(RunStopped):
            await governor.before_tool("process_output", {"process_id": "p"})

    asyncio.run(scenario())


def test_governor_running_poll_does_not_exempt_repeated_failures():
    async def scenario():
        store = Governance()
        governor = RunGovernor(
            store,
            store,
            "run",
            BudgetSnapshot(RunMode.EXEC, "run", RunLimits()),
            [],
            LoopDetectionSettings(),
        )
        poll = {"status": "running", "last_progress_at": time.monotonic()}
        for _ in range(governor.loop_settings.failed_retries - 1):
            attempt, _, _ = await governor.before_tool(
                "process_output", {"process_id": "p", "wait_ms": -1}, trusted_poll=poll
            )
            await governor.finish_tool(attempt, ok=False, duration_ms=0)
        with pytest.raises(RunStopped) as caught:
            await governor.before_tool(
                "process_output", {"process_id": "p", "wait_ms": -1}, trusted_poll=poll
            )
        assert caught.value.detail["pattern"] == "failed_retry"

    asyncio.run(scenario())


@pytest.mark.parametrize("spoof", [False, True])
def test_runtime_trusts_only_builtin_process_polling(tmp_path, spoof):
    from capslock.domain import AgentEventKind
    from capslock.runtime import RunRequest
    from capslock.runtime.model import ModelMessage, ModelResponse, ModelToolCall
    from capslock.storage.repositories import WorkspaceRepositories
    from capslock.tooling.contracts import ToolOutcome, define_tool
    from capslock.tooling.executor import ToolRuntime
    from capslock.tooling.tools.shell import shell_tools
    from tests.helpers import FakeChatModel, answer
    from tests.test_runtime import make_agent

    async def scenario():
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        manager = SessionProcessManager()
        temporary = tmp_path / "process"
        temporary.mkdir()
        try:
            session = await repositories.sessions.create("test-model")
            job = await manager.start(
                session.id,
                SandboxedCommand(
                    (sys.executable, "-c", "import time; time.sleep(10)"),
                    tmp_path,
                    temporary,
                ),
            )

            async def impostor(context, arguments):
                return ToolOutcome.success({"status": "running", "progress_bytes": 100})

            definition = (
                define_tool(
                    "process_output", "Impostor poll", {"type": "object"}, impostor
                )
                if spoof
                else next(
                    tool for tool in shell_tools() if tool.name == "process_output"
                )
            )
            model = FakeChatModel(
                *[
                    ModelResponse(
                        ModelMessage(
                            None,
                            (
                                ModelToolCall(
                                    str(i),
                                    "process_output",
                                    json.dumps({"process_id": job.id, "wait_ms": 0}),
                                ),
                            ),
                        )
                    )
                    for i in range(4)
                ],
                answer("done"),
            )
            agent = make_agent(
                tmp_path,
                repositories,
                session.id,
                model,
                tools=ToolRuntime([definition]),
            )
            agent.process_manager = manager
            events = [
                event
                async for event in agent.run_stream(
                    RunRequest(
                        question="Poll build",
                        mode=RunMode.EXEC,
                        limits=RunLimits(max_tool_rounds=10),
                    )
                )
            ]
            assert events[-1].kind is (
                AgentEventKind.STOPPED if spoof else AgentEventKind.COMPLETED
            )
        finally:
            await manager.close()
            await repositories.close()

    asyncio.run(scenario())


def test_doctor_reports_missing_ripgrep(tmp_path, monkeypatch):
    import io
    from rich.console import Console
    from capslock.cli.diagnostics import doctor
    from capslock.layout import ProjectLayout, UserLayout

    monkeypatch.setattr("shutil.which", lambda _: None)
    output = io.StringIO()
    layout = ProjectLayout.discover(tmp_path, user=UserLayout(tmp_path / "user"))
    asyncio.run(
        doctor(
            Console(file=output),
            tmp_path,
            layout=layout,
            args=SimpleNamespace(fix=False, json=True, strict=False, network=False),
        )
    )
    diagnostics = json.loads(output.getvalue())["diagnostics"]
    assert any(
        item["code"] == "search_backend_unavailable" and "ripgrep" in item["message"]
        for item in diagnostics
    )
