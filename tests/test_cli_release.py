from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.cli import app as cli
from capslock.session import SessionStore
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
        "CAPSLOCK_MEMORY_DATABASE",
        "CAPSLOCK_SKILLS_DIRECTORY",
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
    assert capsys.readouterr().out.strip() == "capslock 1.5.0"


def test_bare_cli_starts_chat_and_explicit_chat_remains_supported(tmp_path: Path, monkeypatch) -> None:
    started = []

    @contextmanager
    def create_application(workspace, settings, session_id=None, *, layout=None):
        yield SimpleNamespace(agent=object())

    monkeypatch.setattr(cli, "load_project_environment", lambda workspace: None)
    monkeypatch.setattr(cli.Settings, "load", lambda workspace, *, layout=None: object())
    monkeypatch.setattr(cli, "create_application", create_application)
    monkeypatch.setattr(cli, "run_chat", lambda context, debug: started.append(debug) or 0)

    assert cli.main(["--workspace", str(tmp_path)]) == 0
    assert cli.main(["--workspace", str(tmp_path), "chat"]) == 0
    assert started == [False, False]


def test_cli_loads_environment_from_selected_workspace(tmp_path: Path, monkeypatch) -> None:
    clear_config_environment(monkeypatch)
    launch_directory = tmp_path / "launch"
    workspace = tmp_path / "workspace"
    launch_directory.mkdir()
    workspace.mkdir()
    (launch_directory / ".env").write_text("CAPSLOCK_MODEL=wrong-model\n", encoding="utf-8")
    (workspace / ".env").write_text("CAPSLOCK_MODEL=workspace-model\n", encoding="utf-8")
    observed = {}

    def doctor(console, selected_workspace, settings, *, layout=None):
        observed["workspace"] = selected_workspace
        observed["model"] = settings.model
        return 0

    monkeypatch.chdir(launch_directory)
    monkeypatch.setattr(cli, "doctor", doctor)

    assert cli.main(["--workspace", str(workspace), "doctor"]) == 0
    assert observed == {"workspace": workspace.resolve(), "model": "workspace-model"}


def test_sessions_rename_accepts_unique_id_prefix_and_lists_title(tmp_path: Path, monkeypatch) -> None:
    clear_config_environment(monkeypatch)
    with SessionStore(tmp_path / ".capslock" / "capslock.sqlite3") as store:
        session = store.create(tmp_path, "test")
    terminal = make_console(width=120, color_system=None, force_terminal=False, record=True)

    assert cli.main(
        ["--workspace", str(tmp_path), "sessions", "rename", session.id[:8], "Release", "planning"],
        console=terminal,
    ) == 0
    assert cli.main(["--workspace", str(tmp_path), "sessions"], console=terminal) == 0

    with SessionStore(tmp_path / ".capslock" / "capslock.sqlite3") as store:
        assert store.get(session.id).title == "Release planning"
    output = terminal.export_text()
    assert "Session renamed: Release planning" in output
    assert "Release planning" in output and session.id[:12] in output


def test_resume_accepts_unique_session_id_prefix(tmp_path: Path, monkeypatch) -> None:
    with SessionStore(tmp_path / ".capslock" / "capslock.sqlite3") as store:
        session = store.create(tmp_path, "test")
    resumed = []

    @contextmanager
    def create_application(workspace, settings, session_id=None, *, layout=None):
        resumed.append(session_id)
        yield SimpleNamespace(agent=object())

    monkeypatch.setattr(cli, "load_project_environment", lambda workspace: None)
    monkeypatch.setattr(cli.Settings, "load", lambda workspace, *, layout=None: object())
    monkeypatch.setattr(cli, "create_application", create_application)
    monkeypatch.setattr(cli, "run_chat", lambda context, debug: 0)

    assert cli.main(["--workspace", str(tmp_path), "resume", session.id[:8]]) == 0
    assert resumed == [session.id]


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
    monkeypatch.setenv("CAPSLOCK_MEMORY_DATABASE", str(tmp_path / "user-memory.sqlite3"))
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
