from pathlib import Path

import pytest

from capslock.config import Settings
from capslock.environment import load_project_environment
from capslock.policy import PolicyError, WorkspacePolicy
from capslock.tools import RunContext, workspace_tools


def context(workspace: Path) -> RunContext:
    return RunContext("session", "run", WorkspacePolicy(workspace), 6, lambda *args, **kwargs: None)


def test_search_lists_only_supported_text_files(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def useful_function():\n    pass\n", encoding="utf-8")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01")
    result, _ = workspace_tools().invoke("search_files", context(tmp_path), {"path": ".", "query": "useful_function"})
    assert result.ok
    assert [item["path"] for item in result.data] == [str((tmp_path / "main.py").resolve())]


def test_read_rejects_binary_and_oversized_files(tmp_path: Path) -> None:
    (tmp_path / "binary.txt").write_bytes(b"\xff")
    policy = WorkspacePolicy(tmp_path, max_file_bytes=3)
    with pytest.raises(PolicyError, match="non-UTF-8"):
        policy.readable_file("binary.txt")
    (tmp_path / "large.txt").write_text("four", encoding="utf-8")
    with pytest.raises(PolicyError, match="read limit"):
        policy.readable_file("large.txt")


def test_environment_and_toml_settings_have_expected_precedence(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env.example").write_text("FROM_EXAMPLE=yes\n", encoding="utf-8")
    (tmp_path / ".env").write_text("FROM_LOCAL=yes\n", encoding="utf-8")
    (tmp_path / "capslock.toml").write_text("[model]\nmodel = 'toml-model'\nmax_turns = 3\n", encoding="utf-8")
    monkeypatch.delenv("FROM_EXAMPLE", raising=False)
    monkeypatch.delenv("FROM_LOCAL", raising=False)
    monkeypatch.setenv("CAPSLOCK_MODEL", "environment-model")
    load_project_environment(tmp_path)
    settings = Settings.load(tmp_path)
    assert settings.model == "environment-model"
    assert settings.max_turns == 3
    assert __import__("os").environ["FROM_EXAMPLE"] == "yes"
    assert __import__("os").environ["FROM_LOCAL"] == "yes"
