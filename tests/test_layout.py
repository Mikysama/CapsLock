from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from capslock.cli import app as cli
from capslock.environment import load_project_environment
from capslock.layout import LayoutConflict, LayoutMigrator, ProjectLayout, UserLayout
from capslock.theme import make_console


def user_layout(tmp_path: Path, *, override: Path | None = None) -> UserLayout:
    return UserLayout(
        tmp_path / "user-home",
        tmp_path / "legacy-user" / "skills",
        tmp_path / "legacy-user" / "memory.sqlite3",
        override,
    )


def project_layout(tmp_path: Path, *, override: Path | None = None) -> ProjectLayout:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ProjectLayout.discover(workspace, user=user_layout(tmp_path, override=override))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_layout_prefers_new_paths_and_reads_identical_legacy_config(tmp_path: Path) -> None:
    layout = project_layout(tmp_path)
    layout.root.mkdir()
    layout.root.joinpath("config.toml").write_text("[model]\nmodel='new'\n", encoding="utf-8")
    layout.workspace.joinpath("capslock.toml").write_text("[model]\nmodel='new'\n", encoding="utf-8")

    assert layout.config == layout.root / "config.toml"
    assert layout.database == layout.root / "state" / "capslock.sqlite3"
    assert layout.mode == "mixed"


def test_layout_rejects_config_and_state_splits(tmp_path: Path) -> None:
    layout = project_layout(tmp_path)
    layout.root.mkdir()
    layout.root.joinpath("config.toml").write_text("new", encoding="utf-8")
    layout.workspace.joinpath("capslock.toml").write_text("old", encoding="utf-8")
    with pytest.raises(LayoutConflict, match="conflicting new and legacy project config"):
        _ = layout.config

    layout.root.joinpath("capslock.sqlite3").write_bytes(b"old")
    layout.root.joinpath("state").mkdir()
    layout.root.joinpath("state/capslock.sqlite3").write_bytes(b"new")
    with pytest.raises(LayoutConflict, match="both new and legacy workspace state"):
        _ = layout.database


def test_layout_rejects_symlinked_managed_parent_directories(tmp_path: Path) -> None:
    layout = project_layout(tmp_path)
    layout.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    layout.root.joinpath("state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LayoutConflict, match="path must not contain a symlink"):
        _ = layout.database

    other = tmp_path / "other-workspace"
    other.mkdir()
    other.joinpath(".capslock").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LayoutConflict, match="project .capslock directory"):
        ProjectLayout.discover(other, user=user_layout(tmp_path))


def test_user_layout_validates_shell_paths_and_memory_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CAPSLOCK_HOME", "relative")
    with pytest.raises(ValueError, match="CAPSLOCK_HOME must be an absolute path"):
        UserLayout.from_environment()

    monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CAPSLOCK_MEMORY_DATABASE", "relative.sqlite3")
    with pytest.raises(ValueError, match="CAPSLOCK_MEMORY_DATABASE must be an absolute path"):
        UserLayout.from_environment()

    override = tmp_path / "memory.sqlite3"
    monkeypatch.setenv("CAPSLOCK_MEMORY_DATABASE", str(override))
    assert UserLayout.from_environment().memory == override


def test_project_environment_cannot_redirect_user_storage(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CAPSLOCK_HOME", raising=False)
    monkeypatch.delenv("CAPSLOCK_MEMORY_DATABASE", raising=False)
    monkeypatch.delenv("CAPSLOCK_MODEL", raising=False)
    tmp_path.joinpath(".env").write_text(
        "CAPSLOCK_HOME=/tmp/redirected\nCAPSLOCK_MEMORY_DATABASE=/tmp/memory.sqlite3\nCAPSLOCK_MODEL=loaded\n",
        encoding="utf-8",
    )

    load_project_environment(tmp_path)

    assert "CAPSLOCK_HOME" not in __import__("os").environ
    assert "CAPSLOCK_MEMORY_DATABASE" not in __import__("os").environ
    assert __import__("os").environ["CAPSLOCK_MODEL"] == "loaded"


def test_migration_dry_run_and_apply_preserve_files(tmp_path: Path) -> None:
    layout = project_layout(tmp_path)
    layout.root.mkdir()
    database = layout.root / "capslock.sqlite3"
    events = layout.root / "events.jsonl"
    database.write_bytes(b"sqlite-data")
    events.write_text('{"event":1}\n', encoding="utf-8")
    skill = layout.legacy_skills / "demo"
    skill.mkdir(parents=True)
    skill.joinpath("instructions.md").write_text("instruction", encoding="utf-8")
    before = {database.name: digest(database), events.name: digest(events)}

    migrator = LayoutMigrator(layout)
    plan = migrator.plan()

    assert {item.status for item in plan.changes} == {"copy"}
    assert database.exists() and not (layout.root / "state/capslock.sqlite3").exists()

    final = migrator.apply(plan)

    assert not final.changes and not database.exists() and not layout.legacy_skills.exists()
    assert digest(layout.root / "state/capslock.sqlite3") == before[database.name]
    assert digest(layout.root / "state/events.jsonl") == before[events.name]
    assert (layout.skills / "demo/instructions.md").read_text(encoding="utf-8") == "instruction"
    assert not LayoutMigrator(layout).plan().changes


def test_migration_merges_directories_and_recovers_identical_target(tmp_path: Path) -> None:
    layout = project_layout(tmp_path)
    old = layout.legacy_skills
    new = layout.skills
    old.joinpath("one").mkdir(parents=True)
    old.joinpath("one/a.md").write_text("same", encoding="utf-8")
    old.joinpath("two").mkdir()
    old.joinpath("two/b.md").write_text("old-only", encoding="utf-8")
    new.joinpath("one").mkdir(parents=True)
    new.joinpath("one/a.md").write_text("same", encoding="utf-8")
    new.joinpath("three").mkdir()
    new.joinpath("three/c.md").write_text("new-only", encoding="utf-8")

    migrator = LayoutMigrator(layout)
    item = next(item for item in migrator.plan().items if item.kind == "project Skills")
    assert item.status == "merge"

    migrator.apply(migrator.plan())

    assert not old.exists()
    assert new.joinpath("two/b.md").read_text(encoding="utf-8") == "old-only"
    assert new.joinpath("three/c.md").read_text(encoding="utf-8") == "new-only"


def test_migration_conflict_or_symlink_has_zero_writes(tmp_path: Path) -> None:
    layout = project_layout(tmp_path)
    layout.legacy_skills.joinpath("demo").mkdir(parents=True)
    layout.legacy_skills.joinpath("demo/instructions.md").write_text("old", encoding="utf-8")
    layout.skills.joinpath("demo").mkdir(parents=True)
    layout.skills.joinpath("demo/instructions.md").write_text("new", encoding="utf-8")
    legacy_config = layout.workspace / "capslock.toml"
    legacy_config.write_text("config", encoding="utf-8")

    plan = LayoutMigrator(layout).plan()
    assert plan.conflicts
    with pytest.raises(LayoutConflict):
        LayoutMigrator(layout).apply(plan)
    assert legacy_config.exists() and not (layout.root / "config.toml").exists()

    layout.legacy_skills.joinpath("demo/instructions.md").unlink()
    layout.legacy_skills.joinpath("demo/instructions.md").symlink_to(legacy_config)
    assert next(
        item for item in LayoutMigrator(layout).plan().items if item.kind == "project Skills"
    ).status == "conflict"


def test_user_and_all_scope_respect_explicit_memory_override(tmp_path: Path) -> None:
    override = tmp_path / "external-memory.sqlite3"
    layout = project_layout(tmp_path, override=override)
    layout.user.legacy_skills.joinpath("demo").mkdir(parents=True)
    layout.user.legacy_skills.joinpath("demo/skill.toml").write_text("skill", encoding="utf-8")
    layout.user.legacy_memory.parent.mkdir(parents=True, exist_ok=True)
    layout.user.legacy_memory.write_bytes(b"memory")

    user_plan = LayoutMigrator(layout).plan("user")

    assert {item.kind for item in user_plan.items} == {"user Skills"}
    LayoutMigrator(layout).apply(user_plan)
    assert layout.user.skills.joinpath("demo/skill.toml").exists()
    assert layout.user.legacy_memory.exists()
    assert not layout.user.canonical_memory.exists()


def test_migrate_cli_short_circuits_configuration_model_and_sqlite(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.joinpath(".capslock").mkdir(parents=True)
    workspace.joinpath(".capslock/events.jsonl").write_text("event\n", encoding="utf-8")
    monkeypatch.setattr(cli, "load_project_environment", lambda workspace: pytest.fail("loaded .env"))
    monkeypatch.setattr(cli.Settings, "load", lambda *args, **kwargs: pytest.fail("loaded settings"))
    monkeypatch.setattr(cli, "create_application", lambda *args, **kwargs: pytest.fail("created application"))
    terminal = make_console(width=160, color_system=None, force_terminal=False, record=True)

    assert cli.main(["--workspace", str(workspace), "migrate-layout"], console=terminal) == 0
    assert workspace.joinpath(".capslock/events.jsonl").exists()
    assert not workspace.joinpath(".capslock/state").exists()

    assert cli.main(
        ["--workspace", str(workspace), "migrate-layout", "--apply", "--yes"],
        console=terminal,
    ) == 0
    assert workspace.joinpath(".capslock/state/events.jsonl").exists()
    assert not workspace.joinpath(".capslock/events.jsonl").exists()
    assert not workspace.joinpath(".capslock/state/capslock.sqlite3").exists()
