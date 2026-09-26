"""Glob backend errors, path semantics, and bounded process lifetime."""

from __future__ import annotations

import asyncio
import sys

import pytest

from capslock.policy import WorkspacePolicy
from capslock.tooling.contracts import ExecutionContext
from capslock.tooling.tools.filesystem.search import glob_files


def context(root, *, max_files=1000):
    return ExecutionContext(
        session_id="session",
        run_id="run",
        policy=WorkspacePolicy(root, max_files=max_files),
        event=lambda *args, **kwargs: None,
        actions=object(),
    )


def test_glob_missing_backend_is_explicit_failure(tmp_path, monkeypatch):
    (tmp_path / "one.txt").write_text("one")
    monkeypatch.setattr(
        "capslock.tooling.tools.filesystem.search.shutil.which", lambda _: None
    )
    result = asyncio.run(glob_files(context(tmp_path), {"pattern": "*"}))
    assert not result.ok
    assert result.error_code == "search_backend_unavailable"


def test_glob_start_failure_is_explicit(tmp_path, monkeypatch):
    async def fail(*args, **kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    result = asyncio.run(glob_files(context(tmp_path), {"pattern": "*"}))
    assert not result.ok
    assert result.error_code == "search_backend_unavailable"


def test_glob_invalid_pattern_reports_backend_failure(tmp_path):
    result = asyncio.run(glob_files(context(tmp_path), {"pattern": "["}))
    assert not result.ok
    assert result.error_code == "search_failed"
    assert result.data["exit_code"] == 2


def test_glob_ignores_user_config(tmp_path, monkeypatch):
    config = tmp_path / "rg.conf"
    config.write_text("--no-ignore\n")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "one.txt").write_text("one")
    (tmp_path / "one.txt").write_text("one")
    result = asyncio.run(glob_files(context(tmp_path), {"pattern": "*"}))
    assert result.ok
    assert result.data["files"] == ["one.txt", "rg.conf"]


def test_glob_explicit_pattern_keeps_rg_ignore_precedence(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("one.txt\n")
    (tmp_path / "one.txt").write_text("one")
    (tmp_path / ".hidden.txt").write_text("one")
    result = asyncio.run(glob_files(context(tmp_path), {"pattern": "*.txt"}))
    assert result.ok
    assert result.data["files"] == ["one.txt"]


def test_glob_subdirectory_paths_are_workspace_relative(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "one.txt").write_text("one")
    result = asyncio.run(
        glob_files(context(tmp_path), {"pattern": "*", "path": "nested"})
    )
    assert result.ok
    assert result.data["files"] == ["nested/one.txt"]


def test_glob_preserves_newlines_in_filenames(tmp_path):
    (tmp_path / "two\nlines.txt").write_text("one")
    result = asyncio.run(glob_files(context(tmp_path), {"pattern": "*.txt"}))
    assert result.ok
    assert result.data["files"] == ["two\nlines.txt"]


def test_glob_hidden_ignore_and_privacy(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "one.txt").write_text("one")
    (tmp_path / ".hidden.txt").write_text("one")
    (tmp_path / ".env").write_text("private")
    (tmp_path / ".capslock").mkdir()
    (tmp_path / ".capslock" / "private.txt").write_text("private")
    (tmp_path / "one.txt").write_text("one")
    hidden = asyncio.run(
        glob_files(context(tmp_path), {"pattern": "*", "include_hidden": True})
    )
    default = asyncio.run(glob_files(context(tmp_path), {"pattern": "*"}))
    assert default.data["files"] == ["one.txt"]
    assert hidden.data["files"] == [".gitignore", ".hidden.txt", "one.txt"]


def test_glob_exact_result_limit_is_not_truncated(tmp_path):
    (tmp_path / "one.txt").write_text("one")
    result = asyncio.run(glob_files(context(tmp_path), {"pattern": "*", "limit": 1}))
    assert result.ok
    assert result.data["files"] == ["one.txt"]
    assert result.data["truncated"] is False


def test_glob_scan_budget_bounds_filtered_results(tmp_path):
    for number in range(6):
        (tmp_path / f".env.{number}").write_text("private")
    result = asyncio.run(
        glob_files(
            context(tmp_path, max_files=2),
            {"pattern": "*", "include_hidden": True},
        )
    )
    assert result.ok and result.data["files"] == []
    assert result.data["truncated"] is True
    assert result.data["stop_reason"] == "scan_limit"


@pytest.mark.parametrize("cancel", [False, True])
def test_glob_bounds_collection_and_cleans_child(tmp_path, monkeypatch, cancel):
    async def scenario():
        for name in ("one.txt", "two.txt"):
            (tmp_path / name).write_text("one")
        original = asyncio.create_subprocess_exec
        spawned = []
        ready = asyncio.Event()
        paths = str(tmp_path / "one.txt") + "\0" + str(tmp_path / "two.txt") + "\0"

        async def spawn(*args, **kwargs):
            script = (
                "import sys, time; "
                f"sys.stdout.write({paths!r} * 100000); "
                "sys.stdout.flush(); time.sleep(10)"
            )
            process = await original(sys.executable, "-c", script, **kwargs)
            spawned.append(process)
            ready.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(
            glob_files(context(tmp_path), {"pattern": "*", "limit": 1})
        )
        try:
            await ready.wait()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                async with asyncio.timeout(3):
                    result = await task
                assert result.ok and result.data["files"] == ["one.txt"]
                assert result.data["truncated"] is True
                assert result.data["stop_reason"] == "result_limit"
            assert spawned[0].returncode is not None
        finally:
            for process in spawned:
                if process.returncode is None:
                    process.kill()
                await process.communicate()

    asyncio.run(scenario())
