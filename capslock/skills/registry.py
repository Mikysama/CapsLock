"""User/workspace Skill discovery, precedence, and local disable state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import __version__
from ..layout import ProjectLayout, UserLayout
from .manifest import SkillPackage, SkillValidationError, load_skill_package


@dataclass(frozen=True)
class SkillEntry:
    name: str
    scope: str
    path: Path
    enabled: bool
    package: SkillPackage | None = None
    error: str | None = None


def default_user_skill_directory() -> Path:
    return UserLayout.from_environment().skills


class SkillRegistry:
    def __init__(
        self,
        workspace: Path,
        *,
        available_tools: set[str],
        disabled: Callable[[str], bool] | None = None,
        user_root: Path | None = None,
        current_version: str = __version__,
        layout: ProjectLayout | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.layout = layout or ProjectLayout.discover(self.workspace)
        self.workspace_root = self.layout.skills
        self.legacy_workspace_root = self.layout.legacy_skills
        self.user_root = user_root or self.layout.user.skills
        self.legacy_user_root = None if user_root is not None else self.layout.user.legacy_skills
        self.available_tools = available_tools
        self.is_disabled = disabled or (lambda name: False)
        self.current_version = current_version

    def entries(self) -> list[SkillEntry]:
        user = self._merged_scope(
            self._discover(self.user_root, "user"),
            self._discover(self.legacy_user_root, "user") if self.legacy_user_root else {},
            "user",
        )
        workspace = self._merged_scope(
            self._discover(self.workspace_root, "workspace"),
            self._discover(self.legacy_workspace_root, "workspace"),
            "workspace",
        )
        merged = {**user, **workspace}
        return [merged[name] for name in sorted(merged)]

    def get(self, name: str, *, require_enabled: bool = True) -> SkillPackage:
        entry = next((item for item in self.entries() if item.name == name), None)
        if entry is None:
            raise SkillValidationError(f"Skill is not registered: {name}")
        if entry.error:
            raise SkillValidationError(entry.error)
        if require_enabled and not entry.enabled:
            raise SkillValidationError(f"Skill is disabled in this workspace: {name}")
        if entry.package is None:
            raise SkillValidationError(f"Skill is unavailable: {name}")
        return entry.package

    def validate_snapshot(self, package: SkillPackage) -> None:
        current = self.get(package.name)
        if current.digest != package.digest or current.scope != package.scope:
            raise SkillValidationError(f"Skill changed while running: {package.name}")

    def _discover(self, root: Path, scope: str) -> dict[str, SkillEntry]:
        if not root.exists():
            return {}
        if root.is_symlink() or not root.is_dir():
            return {root.name: SkillEntry(root.name, scope, root, False, error=f"Skill registry must be a regular directory: {root}")}
        output: dict[str, SkillEntry] = {}
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                continue
            enabled = not self.is_disabled(path.name)
            try:
                package = load_skill_package(
                    path,
                    scope=scope,
                    current_version=self.current_version,
                    available_tools=self.available_tools,
                )
            except SkillValidationError as exc:
                output[path.name] = SkillEntry(path.name, scope, path, False, error=str(exc))
            else:
                output[package.name] = SkillEntry(package.name, scope, path, enabled, package=package)
        return output

    def _merged_scope(
        self,
        current: dict[str, SkillEntry],
        legacy: dict[str, SkillEntry],
        scope: str,
    ) -> dict[str, SkillEntry]:
        merged = dict(legacy)
        for name, entry in current.items():
            previous = legacy.get(name)
            if previous is None:
                merged[name] = entry
                continue
            same = (
                entry.error is None
                and previous.error is None
                and entry.package is not None
                and previous.package is not None
                and entry.package.digest == previous.package.digest
            )
            if same:
                merged[name] = entry
                continue
            merged[name] = SkillEntry(
                name,
                scope,
                entry.path,
                False,
                error=(
                    f"conflicting new and legacy {scope} Skill '{name}': "
                    f"{entry.path} and {previous.path}; run capslock migrate-layout"
                ),
            )
        return merged
