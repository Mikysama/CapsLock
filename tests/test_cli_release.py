from pathlib import Path

import pytest

from capslock.cli import app as cli
from capslock.theme import make_console


def clear_config_environment(monkeypatch) -> None:
    for name in (
        "CAPSLOCK_API_KEY",
        "DEEPSEEK_API_KEY",
        "CAPSLOCK_BASE_URL",
        "DEEPSEEK_BASE_URL",
        "CAPSLOCK_MODEL",
        "DEEPSEEK_MODEL",
        "CAPSLOCK_TAVILY_API_KEY",
        "TAVILY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_help_and_version_do_not_load_workspace_environment(monkeypatch, capsys) -> None:
    def unexpected_load(*args, **kwargs):
        pytest.fail("configuration was loaded before argparse exited")

    monkeypatch.setattr(cli, "load_project_environment", unexpected_load)
    with pytest.raises(SystemExit) as help_exit:
        cli.main(["--help"])
    assert help_exit.value.code == 0
    assert "doctor" in capsys.readouterr().out

    with pytest.raises(SystemExit) as version_exit:
        cli.main(["--version"])
    assert version_exit.value.code == 0
    assert capsys.readouterr().out.strip() == "capslock 1.3.2"


def test_cli_loads_environment_from_selected_workspace(tmp_path: Path, monkeypatch) -> None:
    clear_config_environment(monkeypatch)
    launch_directory = tmp_path / "launch"
    workspace = tmp_path / "workspace"
    launch_directory.mkdir()
    workspace.mkdir()
    (launch_directory / ".env").write_text("CAPSLOCK_MODEL=wrong-model\n", encoding="utf-8")
    (workspace / ".env").write_text("CAPSLOCK_MODEL=workspace-model\n", encoding="utf-8")
    observed = {}

    def doctor(console, selected_workspace, settings):
        observed["workspace"] = selected_workspace
        observed["model"] = settings.model
        return 0

    monkeypatch.chdir(launch_directory)
    monkeypatch.setattr(cli, "doctor", doctor)

    assert cli.main(["--workspace", str(workspace), "doctor"]) == 0
    assert observed == {"workspace": workspace.resolve(), "model": "workspace-model"}


@pytest.mark.parametrize(
    ("git_repository", "api_key", "expected_git", "expected_api"),
    [
        (True, "configured-secret", "repository", "configured"),
        (False, None, "not a repository", "missing"),
    ],
)
def test_doctor_reports_capabilities_without_leaking_secrets(
    tmp_path: Path,
    monkeypatch,
    git_repository: bool,
    api_key: str | None,
    expected_git: str,
    expected_api: str,
) -> None:
    clear_config_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n", encoding="utf-8")
    if git_repository:
        (workspace / ".git").mkdir()
    if api_key:
        monkeypatch.setenv("CAPSLOCK_API_KEY", api_key)
    monkeypatch.setenv(
        "CAPSLOCK_BASE_URL",
        "https://endpoint-user:endpoint-password@example.com/api?token=query-secret",
    )
    terminal = make_console(width=120, color_system=None, force_terminal=False, record=True)

    exit_code = cli.main(["--workspace", str(workspace), "doctor"], console=terminal)
    output = terminal.export_text()

    assert exit_code == 0
    assert expected_git in output
    assert expected_api in output
    assert "pytest" in output and "ruff_check" in output
    assert "https://example.com/api" in output
    for secret in (api_key, "endpoint-user", "endpoint-password", "query-secret"):
        if secret:
            assert secret not in output
