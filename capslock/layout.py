"""Canonical project/user layout discovery and explicit legacy migration."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


class LayoutConflict(RuntimeError):
    """New and legacy layout data cannot be resolved without user action."""


@dataclass(frozen=True)
class UserLayout:
    home: Path
    legacy_memory: Path
    memory_override: Path | None = None

    @classmethod
    def from_environment(cls) -> "UserLayout":
        configured = os.environ.get("CAPSLOCK_HOME")
        if configured:
            home = Path(configured).expanduser()
            if not home.is_absolute():
                raise ValueError("CAPSLOCK_HOME must be an absolute path")
        else:
            home = Path.home() / ".capslock"
        override_value = os.environ.get("CAPSLOCK_MEMORY_DATABASE")
        override = None
        if override_value:
            override = Path(override_value).expanduser()
            if not override.is_absolute():
                raise ValueError("CAPSLOCK_MEMORY_DATABASE must be an absolute path")
        if sys.platform == "darwin":
            application_support = Path.home() / "Library" / "Application Support"
            legacy_memory = application_support / "CapsLock" / "memory.sqlite3"
        else:
            data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            legacy_root = data_home / "capslock"
            legacy_memory = legacy_root / "memory.sqlite3"
        return cls(home.resolve(), legacy_memory, override)

    @property
    def skills(self) -> Path:
        return self.home / "skills"

    @property
    def canonical_memory(self) -> Path:
        return self.home / "state" / "memory.sqlite3"

    @property
    def memory(self) -> Path:
        if self.memory_override is not None:
            return self.memory_override
        new, legacy = self.canonical_memory, self.legacy_memory
        _reject_parent_symlinks(new, self.home, "user memory database")
        _reject_symlink(new, "user memory database")
        _reject_symlink(legacy, "legacy user memory database")
        if new.exists() and legacy.exists():
            raise LayoutConflict(
                f"both new and legacy user memory databases exist: {new} and {legacy}; "
                "run capslock migrate-layout --scope user"
            )
        return legacy if legacy.exists() else new


@dataclass(frozen=True)
class ProjectLayout:
    workspace: Path
    user: UserLayout

    @classmethod
    def discover(cls, workspace: Path, *, user: UserLayout | None = None) -> "ProjectLayout":
        layout = cls(workspace.resolve(), user or UserLayout.from_environment())
        _reject_symlink(layout.root, "project .capslock directory")
        return layout

    @property
    def root(self) -> Path:
        return self.workspace / ".capslock"

    @property
    def config(self) -> Path:
        return self._compatible_file(self.root / "config.toml", self.workspace / "capslock.toml", "project config")

    @property
    def project_mcp(self) -> Path:
        return self._compatible_file(self.root / "mcp.json", self.workspace / "capslock.mcp.json", "project MCP config")

    @property
    def local_mcp(self) -> Path:
        return self._compatible_file(self.root / "local" / "mcp.json", self.root / "mcp.local.json", "local MCP config")

    @property
    def skills(self) -> Path:
        path = self.root / "skills"
        _reject_parent_symlinks(path, self.root, "project Skills")
        return path

    @property
    def state_mode(self) -> str:
        new_paths = (self.root / "state" / "capslock.sqlite3", self.root / "state" / "events.jsonl", self.root / "state" / "backups")
        legacy_paths = (self.root / "capslock.sqlite3", self.root / "events.jsonl", self.root / "backups")
        for path in (*new_paths, *legacy_paths):
            _reject_parent_symlinks(path, self.root, "workspace state")
            _reject_symlink(path, "workspace state")
        new_present = any(path.exists() for path in new_paths)
        legacy_present = any(path.exists() for path in legacy_paths)
        if new_present and legacy_present:
            raise LayoutConflict(
                "both new and legacy workspace state exist; run capslock migrate-layout --scope workspace"
            )
        return "legacy" if legacy_present else "new"

    @property
    def database(self) -> Path:
        return self.root / ("capslock.sqlite3" if self.state_mode == "legacy" else "state/capslock.sqlite3")

    @property
    def events(self) -> Path:
        return self.root / ("events.jsonl" if self.state_mode == "legacy" else "state/events.jsonl")

    @property
    def mode(self) -> str:
        legacy = any(
            path.exists()
            for path in (
                self.workspace / "capslock.toml",
                self.workspace / "capslock.mcp.json",
                self.root / "mcp.local.json",
                self.root / "capslock.sqlite3",
                self.root / "events.jsonl",
                self.root / "backups",
            )
        )
        new = any(
            path.exists()
            for path in (
                self.root / "config.toml",
                self.root / "mcp.json",
                self.skills,
                self.root / "local" / "mcp.json",
                self.root / "state",
            )
        )
        if legacy and new:
            return "mixed"
        return "legacy" if legacy else "new"

    @property
    def warnings(self) -> tuple[str, ...]:
        warnings: list[str] = []
        if self.mode in {"legacy", "mixed"}:
            warnings.append(
                "legacy CapsLock paths are in use; run `capslock migrate-layout` to preview migration"
            )
        if self.user.memory_override is None and self.user.legacy_memory.exists():
            warnings.append(
                "legacy user data paths are in use; run `capslock migrate-layout --scope user`"
            )
        return tuple(warnings)

    def _compatible_file(self, new: Path, legacy: Path, label: str) -> Path:
        _reject_parent_symlinks(new, self.root, label)
        _reject_symlink(new, label)
        _reject_symlink(legacy, f"legacy {label}")
        if new.exists() and legacy.exists():
            if not new.is_file() or not legacy.is_file() or _file_hash(new) != _file_hash(legacy):
                raise LayoutConflict(
                    f"conflicting new and legacy {label}: {new} and {legacy}; run capslock migrate-layout"
                )
            return new
        return legacy if legacy.exists() else new


@dataclass(frozen=True)
class MigrationItem:
    source: Path
    destination: Path
    kind: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class MigrationPlan:
    scope: str
    items: tuple[MigrationItem, ...]

    @property
    def conflicts(self) -> tuple[MigrationItem, ...]:
        return tuple(item for item in self.items if item.status == "conflict")

    @property
    def changes(self) -> tuple[MigrationItem, ...]:
        return tuple(item for item in self.items if item.status in {"copy", "merge", "cleanup"})


class LayoutMigrator:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout

    def plan(self, scope: str = "workspace") -> MigrationPlan:
        if scope not in {"workspace", "user", "all"}:
            raise ValueError("migration scope must be workspace, user, or all")
        mappings: list[tuple[Path, Path, str]] = []
        if scope in {"workspace", "all"}:
            root = self.layout.root
            mappings.extend(
                (
                    (self.layout.workspace / "capslock.toml", root / "config.toml", "project config"),
                    (self.layout.workspace / "capslock.mcp.json", root / "mcp.json", "project MCP"),
                    (root / "mcp.local.json", root / "local" / "mcp.json", "local MCP"),
                    (root / "capslock.sqlite3", root / "state" / "capslock.sqlite3", "workspace database"),
                    (root / "events.jsonl", root / "state" / "events.jsonl", "workspace events"),
                    (root / "backups", root / "state" / "backups", "workspace backups"),
                )
            )
        if scope in {"user", "all"}:
            user = self.layout.user
            if user.memory_override is None:
                mappings.append((user.legacy_memory, user.canonical_memory, "user memory"))
        return MigrationPlan(scope, tuple(self._inspect(source, destination, kind) for source, destination, kind in mappings))

    def apply(self, plan: MigrationPlan) -> MigrationPlan:
        plan = self.plan(plan.scope)
        if plan.conflicts:
            raise LayoutConflict("layout migration has conflicts; no files were changed")
        active = [item for item in plan.items if item.status in {"copy", "merge", "cleanup"}]
        for item in active:
            if item.status != "cleanup":
                self._copy_verified(item.source, item.destination)
        for item in active:
            self._verify_equivalent(item.source, item.destination)
        for item in active:
            self._remove_source(item.source)
        return self.plan(plan.scope)

    def _inspect(self, source: Path, destination: Path, kind: str) -> MigrationItem:
        if not source.exists() and not source.is_symlink():
            return MigrationItem(source, destination, kind, "missing")
        try:
            self._reject_managed_parent_symlinks(source)
            self._reject_managed_parent_symlinks(destination)
            _reject_tree_symlinks(source)
            if not source.is_file() and not source.is_dir():
                raise LayoutConflict(f"migration source must be a regular file or directory: {source}")
            if destination.exists() or destination.is_symlink():
                _reject_tree_symlinks(destination)
                equivalent, mergeable = _compare_paths(source, destination)
                if equivalent:
                    return MigrationItem(source, destination, kind, "cleanup", "identical target exists")
                if mergeable:
                    return MigrationItem(source, destination, kind, "merge", "non-conflicting directory merge")
                return MigrationItem(source, destination, kind, "conflict", "destination differs")
        except (OSError, LayoutConflict) as exc:
            return MigrationItem(source, destination, kind, "conflict", str(exc))
        return MigrationItem(source, destination, kind, "copy")

    def _copy_verified(self, source: Path, destination: Path) -> None:
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                return
            temporary = destination.with_name(f".migrate-{destination.name}-{uuid.uuid4().hex}")
            shutil.copy2(source, temporary)
            if _file_hash(source) != _file_hash(temporary):
                temporary.unlink(missing_ok=True)
                raise OSError(f"migration verification failed: {source}")
            os.replace(temporary, destination)
            return
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".migrate-{destination.name}-{uuid.uuid4().hex}")
            try:
                shutil.copytree(source, temporary)
                self._verify_equivalent(source, temporary)
                os.replace(temporary, destination)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            return
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            target = destination / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif not target.exists():
                self._copy_verified(path, target)
        destination.mkdir(parents=True, exist_ok=True)

    def _reject_managed_parent_symlinks(self, path: Path) -> None:
        roots = (self.layout.root, self.layout.user.home)
        for root in roots:
            if path != root and root not in path.parents:
                continue
            current = root
            if current.is_symlink():
                raise LayoutConflict(f"migration path must not contain a symlink: {current}")
            for part in path.relative_to(root).parts[:-1]:
                current /= part
                if current.is_symlink():
                    raise LayoutConflict(f"migration path must not contain a symlink: {current}")

    def _verify_equivalent(self, source: Path, destination: Path) -> None:
        equivalent, _ = _compare_paths(source, destination, allow_destination_extras=True)
        if not equivalent:
            raise OSError(f"migration verification failed: {source} -> {destination}")

    def _remove_source(self, source: Path) -> None:
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink(missing_ok=True)


def _compare_paths(source: Path, destination: Path, *, allow_destination_extras: bool = False) -> tuple[bool, bool]:
    if source.is_file() and destination.is_file():
        return _file_hash(source) == _file_hash(destination), False
    if not source.is_dir() or not destination.is_dir():
        return False, False
    source_files = {path.relative_to(source): path for path in source.rglob("*") if path.is_file()}
    destination_files = {path.relative_to(destination): path for path in destination.rglob("*") if path.is_file()}
    overlapping = set(source_files) & set(destination_files)
    if any(_file_hash(source_files[key]) != _file_hash(destination_files[key]) for key in overlapping):
        return False, False
    source_covered = set(source_files) <= set(destination_files)
    if source_covered and (allow_destination_extras or set(source_files) == set(destination_files)):
        return True, True
    return False, True


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise LayoutConflict(f"{label} must not be a symlink: {path}")


def _reject_tree_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise LayoutConflict(f"migration source/target must not be a symlink: {path}")
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_symlink():
                raise LayoutConflict(f"migration source/target contains a symlink: {item}")


def _reject_parent_symlinks(path: Path, root: Path, label: str) -> None:
    current = root
    if current.is_symlink():
        raise LayoutConflict(f"{label} path must not contain a symlink: {current}")
    for part in path.relative_to(root).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise LayoutConflict(f"{label} path must not contain a symlink: {current}")
