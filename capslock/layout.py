"""Canonical CapsLock project and user layout."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class LayoutConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class UserLayout:
    home: Path
    memory_override: Path | None = None

    @classmethod
    def from_environment(cls) -> "UserLayout":
        configured = os.environ.get("CAPSLOCK_HOME")
        home = (
            Path(configured).expanduser() if configured else Path.home() / ".capslock"
        )
        if not home.is_absolute():
            raise ValueError("CAPSLOCK_HOME must be an absolute path")
        override_value = os.environ.get("CAPSLOCK_MEMORY_DATABASE")
        override = Path(override_value).expanduser() if override_value else None
        if override is not None and not override.is_absolute():
            raise ValueError("CAPSLOCK_MEMORY_DATABASE must be an absolute path")
        return cls(home.resolve(), override)

    @property
    def skills(self) -> Path:
        return self.home / "skills"

    @property
    def plugins(self) -> Path:
        return self.home / "plugins"

    @property
    def plugin_registry(self) -> Path:
        return self.home / "state" / "plugins.json"

    @property
    def plugin_audit(self) -> Path:
        return self.home / "state" / "plugin-audit.jsonl"

    @property
    def permissions(self) -> Path:
        return self.home / "permissions.toml"

    @property
    def canonical_memory(self) -> Path:
        return self.home / "state" / "memory.sqlite3"

    @property
    def memory(self) -> Path:
        path = self.memory_override or self.canonical_memory
        root = self.home if path.is_relative_to(self.home) else Path(path.anchor)
        _reject_parent_symlinks(path, root, "user memory database")
        _reject_symlink(path, "user memory database")
        return path


@dataclass(frozen=True)
class ProjectLayout:
    workspace: Path
    user: UserLayout

    @classmethod
    def discover(
        cls, workspace: Path, *, user: UserLayout | None = None
    ) -> "ProjectLayout":
        layout = cls(workspace.resolve(), user or UserLayout.from_environment())
        _reject_symlink(layout.root, "project .capslock directory")
        return layout

    @property
    def root(self) -> Path:
        return self.workspace / ".capslock"

    @property
    def config(self) -> Path:
        return self._managed(self.root / "config.toml", "project config")

    @property
    def project_mcp(self) -> Path:
        return self._managed(self.root / "mcp.json", "project MCP config")

    @property
    def local_mcp(self) -> Path:
        return self._managed(self.root / "local" / "mcp.json", "local MCP config")

    @property
    def project_permissions(self) -> Path:
        return self._managed(self.root / "permissions.toml", "project permissions")

    @property
    def local_permissions(self) -> Path:
        return self._managed(
            self.root / "local" / "permissions.toml", "local permissions"
        )

    @property
    def skills(self) -> Path:
        return self._managed(self.root / "skills", "project Skills")

    @property
    def local_plugins(self) -> Path:
        return self._managed(self.root / "local" / "plugins.json", "local plugins")

    @property
    def database(self) -> Path:
        return self._managed(
            self.root / "state" / "capslock.sqlite3", "workspace database"
        )

    @property
    def events(self) -> Path:
        return self._managed(self.root / "state" / "events.jsonl", "workspace events")

    @property
    def artifacts(self) -> Path:
        return self._managed(
            self.root / "state" / "artifacts", "workspace tool artifacts"
        )

    def _managed(self, path: Path, label: str) -> Path:
        _reject_parent_symlinks(path, self.root, label)
        _reject_symlink(path, label)
        return path


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise LayoutConflict(f"{label} must not be a symlink: {path}")


def _reject_parent_symlinks(path: Path, root: Path, label: str) -> None:
    current = root
    if current.is_symlink():
        raise LayoutConflict(f"{label} path must not contain a symlink: {current}")
    for part in path.relative_to(root).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise LayoutConflict(f"{label} path must not contain a symlink: {current}")
