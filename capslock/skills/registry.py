"""User/workspace Skill discovery, precedence, and catalog rendering."""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..layout import ProjectLayout, UserLayout
from .manifest import SkillPackage, SkillValidationError, load_skill_package


CATALOG_BUDGET_BYTES = 16 * 1024


@dataclass(frozen=True)
class SkillEntry:
    name: str
    scope: str
    path: Path
    enabled: bool
    package: SkillPackage | None = None
    error: str | None = None


@dataclass(frozen=True)
class SkillCatalog:
    text: str
    total: int
    described: int
    truncated: bool
    bytes: int


def default_user_skill_directory() -> Path:
    return UserLayout.from_environment().skills


class SkillRegistry:
    def __init__(
        self,
        workspace: Path,
        *,
        disabled: Callable[[str], bool] | None = None,
        user_root: Path | None = None,
        layout: ProjectLayout | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.layout = layout or ProjectLayout.discover(self.workspace)
        self.workspace_root = self.layout.skills
        self.user_root = user_root or self.layout.user.skills
        self.is_disabled = disabled or (lambda name: False)

    def entries(self) -> list[SkillEntry]:
        user = self._discover(self.user_root, "user")
        workspace = self._discover(self.workspace_root, "workspace")
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

    def catalog(self, *, budget_bytes: int = CATALOG_BUDGET_BYTES) -> SkillCatalog:
        packages = [
            entry.package
            for entry in self.entries()
            if entry.enabled and entry.error is None and entry.package is not None
        ]
        name_lines = [f"- ${package.name}" for package in packages]
        lines = list(name_lines)
        used = len("\n".join(lines).encode("utf-8"))
        described = 0
        for index, package in enumerate(packages):
            description = html.escape(" ".join(package.description.split()))
            candidate = f"- ${package.name}: {description}"
            candidate_bytes = len(candidate.encode("utf-8")) - len(
                name_lines[index].encode("utf-8")
            )
            if used + candidate_bytes > budget_bytes:
                continue
            lines[index] = candidate
            used += candidate_bytes
            described += 1
        text = "\n".join(lines)
        return SkillCatalog(
            text,
            len(packages),
            described,
            described != len(packages),
            len(text.encode("utf-8")),
        )

    def _discover(self, root: Path, scope: str) -> dict[str, SkillEntry]:
        if root.is_symlink():
            return {
                root.name: SkillEntry(
                    root.name,
                    scope,
                    root,
                    False,
                    error=f"Skill registry must be a regular directory: {root}",
                )
            }
        if not root.exists():
            return {}
        if not root.is_dir():
            return {
                root.name: SkillEntry(
                    root.name,
                    scope,
                    root,
                    False,
                    error=f"Skill registry must be a regular directory: {root}",
                )
            }
        output: dict[str, SkillEntry] = {}
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                continue
            enabled = not self.is_disabled(path.name)
            try:
                package = load_skill_package(path, scope=scope)
            except SkillValidationError as exc:
                output[path.name] = SkillEntry(
                    path.name, scope, path, False, error=str(exc)
                )
            else:
                output[package.name] = SkillEntry(
                    package.name,
                    scope,
                    path,
                    enabled,
                    package=package,
                )
        return output
