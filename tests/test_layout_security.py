from pathlib import Path

import pytest

from capslock.application import ActionCoordinator
from capslock.permissions import PermissionMode
from capslock.policy import PolicyError, WorkspacePolicy
from capslock.session import SessionStore
from capslock.tools import RunContext, workspace_tools


def invoke(tmp_path: Path, name: str, arguments: dict[str, object]):
    context = RunContext(
        session_id="session",
        run_id="run",
        policy=WorkspacePolicy(tmp_path),
        event=lambda *args, **kwargs: None,
    )
    return workspace_tools().invoke(name, context, arguments)[0]


def test_agent_reads_project_layout_but_never_private_or_state(tmp_path: Path) -> None:
    root = tmp_path / ".capslock"
    root.joinpath("skills/demo").mkdir(parents=True)
    root.joinpath("state").mkdir()
    root.joinpath("local").mkdir()
    root.joinpath("config.toml").write_text("visible = 'config'", encoding="utf-8")
    root.joinpath("mcp.json").write_text('{"visible":"mcp"}', encoding="utf-8")
    root.joinpath("skills/demo/SKILL.md").write_text("visible skill needle", encoding="utf-8")
    root.joinpath("state/events.jsonl").write_text("private state needle", encoding="utf-8")
    root.joinpath("local/mcp.json").write_text("private local needle", encoding="utf-8")
    root.joinpath("events.jsonl").write_text("legacy state needle", encoding="utf-8")
    tmp_path.joinpath(".env").write_text("CAPSLOCK_API_KEY=private needle", encoding="utf-8")
    tmp_path.joinpath(".env.example").write_text("CAPSLOCK_MODEL=example", encoding="utf-8")

    listed = invoke(tmp_path, "list_files", {"path": "."})
    paths = set(listed.data["files"])
    assert paths >= {
        ".capslock/config.toml",
        ".capslock/mcp.json",
        ".capslock/skills/demo/SKILL.md",
        ".env.example",
    }
    assert ".capslock/state/events.jsonl" not in paths
    assert ".capslock/local/mcp.json" not in paths
    assert ".capslock/events.jsonl" not in paths
    assert ".env" not in paths

    searched = invoke(tmp_path, "search_files", {"path": ".", "query": "needle"})
    assert searched.ok
    assert [item["path"] for item in searched.data] == [
        str(root.joinpath("skills/demo/SKILL.md").resolve())
    ]
    for private in (".capslock/state/events.jsonl", ".capslock/local/mcp.json", ".env"):
        assert not invoke(tmp_path, "read_file", {"path": private}).ok


def test_agent_writes_only_project_skill_content_inside_capslock(tmp_path: Path) -> None:
    policy = WorkspacePolicy(tmp_path)
    tmp_path.joinpath(".capslock/skills/demo").mkdir(parents=True)
    tmp_path.joinpath(".capslock/config.toml").write_text("config", encoding="utf-8")

    assert policy.writable_file(
        ".capslock/skills/demo/SKILL.md", create=True
    ) == tmp_path.joinpath(".capslock/skills/demo/SKILL.md")
    for path in (
        ".capslock/config.toml",
        ".capslock/mcp.json",
        ".capslock/local/mcp.json",
        ".capslock/state/events.jsonl",
    ):
        with pytest.raises(PolicyError, match="only allowed for project Skills"):
            policy.writable_file(path, create=not tmp_path.joinpath(path).exists())


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_skill_file_changes_always_require_explicit_approval(tmp_path: Path, mode: PermissionMode) -> None:
    path = tmp_path / ".capslock/skills/demo/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path / ".capslock/test-state.sqlite3")
    context = RunContext(
        session_id="session",
        run_id="run",
        policy=WorkspacePolicy(tmp_path),
        event=lambda *args, **kwargs: None,
        store=store,
        permission_mode=mode,
    )

    result, _ = workspace_tools().invoke(
        "propose_file_edit",
        context,
        {"path": ".capslock/skills/demo/SKILL.md", "old_text": "before", "new_text": "after"},
    )

    assert result.ok and result.data["status"] == "pending"
    assert path.read_text(encoding="utf-8") == "before"
    coordinator = ActionCoordinator(
        store=store,
        policy=context.policy,
        session_id="session",
        run_id="run",
        event=context.event,
        permission_mode=mode,
    )
    with pytest.raises(ValueError, match="explicit approval"):
        coordinator.execute_auto_approved(result.data["change_id"])
