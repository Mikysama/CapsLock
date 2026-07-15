from pathlib import Path

import pytest

from capslock.config import Settings
from capslock.environment import load_project_environment
from capslock.policy import PolicyError, WorkspacePolicy
from capslock.tools import RunContext, workspace_tools


CONFIG_ENVIRONMENT_NAMES = (
    "CAPSLOCK_API_KEY",
    "DEEPSEEK_API_KEY",
    "CAPSLOCK_BASE_URL",
    "DEEPSEEK_BASE_URL",
    "CAPSLOCK_MODEL",
    "DEEPSEEK_MODEL",
    "CAPSLOCK_TAVILY_API_KEY",
    "TAVILY_API_KEY",
    "CAPSLOCK_MAX_TURNS",
)


def clear_config_environment(monkeypatch) -> None:
    for name in CONFIG_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)


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


def test_dotenv_overrides_toml_and_example_is_not_loaded(tmp_path: Path, monkeypatch) -> None:
    clear_config_environment(monkeypatch)
    (tmp_path / ".env.example").write_text("CAPSLOCK_MODEL=example-model\n", encoding="utf-8")
    (tmp_path / "capslock.toml").write_text("[model]\nmodel = 'toml-model'\nmax_turns = 3\n", encoding="utf-8")

    load_project_environment(tmp_path)
    assert Settings.load(tmp_path).model == "toml-model"

    (tmp_path / ".env").write_text("CAPSLOCK_MODEL=dotenv-model\n", encoding="utf-8")
    load_project_environment(tmp_path)
    settings = Settings.load(tmp_path)

    assert settings.model == "dotenv-model"
    assert settings.max_turns == 3


def test_shell_value_is_not_replaced_by_environment_files(tmp_path: Path, monkeypatch) -> None:
    clear_config_environment(monkeypatch)
    monkeypatch.setenv("CAPSLOCK_MODEL", "shell-model")
    (tmp_path / ".env").write_text("CAPSLOCK_MODEL=dotenv-model\n", encoding="utf-8")

    load_project_environment(tmp_path)

    assert Settings.load(tmp_path).model == "shell-model"


def test_canonical_and_compatible_environment_names_override_toml(tmp_path: Path, monkeypatch) -> None:
    clear_config_environment(monkeypatch)
    (tmp_path / "capslock.toml").write_text(
        "[model]\nmodel = 'toml-model'\nbase_url = 'https://toml.example'\napi_key = 'toml-key'\ntavily_api_key = 'toml-tavily'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_MODEL", "compatible-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://compatible.example")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "compatible-key")
    monkeypatch.setenv("TAVILY_API_KEY", "compatible-tavily")

    compatible = Settings.load(tmp_path)

    assert compatible.model == "compatible-model"
    assert compatible.base_url == "https://compatible.example"
    assert compatible.api_key == "compatible-key"
    assert compatible.tavily_api_key == "compatible-tavily"

    monkeypatch.setenv("CAPSLOCK_MODEL", "canonical-model")
    monkeypatch.setenv("CAPSLOCK_BASE_URL", "https://canonical.example")
    monkeypatch.setenv("CAPSLOCK_API_KEY", "canonical-key")
    monkeypatch.setenv("CAPSLOCK_TAVILY_API_KEY", "canonical-tavily")
    canonical = Settings.load(tmp_path)

    assert canonical.model == "canonical-model"
    assert canonical.base_url == "https://canonical.example"
    assert canonical.api_key == "canonical-key"
    assert canonical.tavily_api_key == "canonical-tavily"


def test_toml_and_defaults_are_used_without_environment(tmp_path: Path, monkeypatch) -> None:
    clear_config_environment(monkeypatch)
    (tmp_path / "capslock.toml").write_text("[model]\nmodel = 'toml-model'\n", encoding="utf-8")
    assert Settings.load(tmp_path).model == "toml-model"

    (tmp_path / "capslock.toml").unlink()
    defaults = Settings.load(tmp_path)
    assert defaults.model == "deepseek-v4-flash"
    assert defaults.base_url == "https://api.deepseek.com"
    assert defaults.api_key is None
