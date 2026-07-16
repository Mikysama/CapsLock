"""Agent Skills compatible SKILL.md parsing and package validation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml


FRONTMATTER_FIELDS = {"name", "description", "license", "compatibility", "metadata"}
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_BYTES = 128 * 1024
MAX_DESCRIPTION_CHARS = 1024
MAX_COMPATIBILITY_CHARS = 500
MAX_RESOURCE_BYTES = 5 * 1024 * 1024
MAX_PACKAGE_BYTES = 10 * 1024 * 1024
MAX_RESOURCES = 1_000


class SkillValidationError(ValueError):
    """A deterministic, user-facing package validation failure."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    loader.flatten_mapping(node)
    output: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in output
        except TypeError as exc:
            raise SkillValidationError("Skill frontmatter keys must be scalar values") from exc
        if duplicate:
            raise SkillValidationError(f"Duplicate Skill frontmatter field: {key}")
        output[key] = loader.construct_object(value_node, deep=deep)
    return output


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


@dataclass(frozen=True)
class SkillResource:
    path: str
    size: int
    kind: str


@dataclass(frozen=True)
class SkillPackage:
    root: Path
    scope: str
    name: str
    description: str
    instructions: str
    digest: str
    resources: tuple[SkillResource, ...]
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] | None = None

    def resource(self, requested_path: str) -> SkillResource:
        normalized = _normalized_resource_path(requested_path)
        resource = next((item for item in self.resources if item.path == normalized), None)
        if resource is None:
            raise SkillValidationError(f"Skill resource does not exist: {requested_path}")
        return resource


def load_skill_package(root: Path, *, scope: str) -> SkillPackage:
    try:
        return _load_skill_package(root, scope=scope)
    except SkillValidationError:
        raise
    except OSError as exc:
        raise SkillValidationError(f"Skill package cannot be read: {root}") from exc


def _load_skill_package(root: Path, *, scope: str) -> SkillPackage:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise SkillValidationError(f"Skill package must be a regular directory: {root}")
    skill_path = root / "SKILL.md"
    if skill_path.is_symlink() or not skill_path.is_file():
        raise SkillValidationError(f"Skill package requires SKILL.md: {root}")
    if skill_path.stat().st_size > MAX_SKILL_BYTES:
        raise SkillValidationError(f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes: {skill_path}")
    try:
        text = skill_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillValidationError(f"SKILL.md must be UTF-8: {skill_path}") from exc
    frontmatter, instructions = _parse_skill_document(text)
    unknown = sorted(str(item) for item in set(frontmatter) - FRONTMATTER_FIELDS)
    if unknown:
        raise SkillValidationError(f"Unsupported Skill frontmatter fields: {', '.join(unknown)}")

    name = _required_string(frontmatter, "name")
    if len(name) > 64 or not NAME_PATTERN.fullmatch(name):
        raise SkillValidationError(
            "Skill name must be 1-64 lowercase letters, numbers, or single hyphen-separated words"
        )
    if root.name != name:
        raise SkillValidationError(f"Skill directory must match frontmatter name: {name}")
    description = _required_string(frontmatter, "description").strip()
    if not description or len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillValidationError("Skill description must contain 1-1024 characters")
    if not instructions.strip():
        raise SkillValidationError("Skill instructions cannot be empty")

    license_value = _optional_string(frontmatter, "license")
    compatibility = _optional_string(frontmatter, "compatibility")
    if compatibility is not None and len(compatibility) > MAX_COMPATIBILITY_CHARS:
        raise SkillValidationError("Skill compatibility must contain at most 500 characters")
    metadata = _metadata(frontmatter.get("metadata"))
    paths = _package_files(root)
    resources = tuple(
        SkillResource(
            path.relative_to(root).as_posix(),
            path.stat().st_size,
            _resource_kind(path.relative_to(root)),
        )
        for path in paths
        if path != skill_path
    )
    return SkillPackage(
        root=root,
        scope=scope,
        name=name,
        description=description,
        instructions=instructions.strip(),
        digest=_digest(root, paths),
        resources=resources,
        license=license_value,
        compatibility=compatibility,
        metadata=metadata,
    )


def _parse_skill_document(text: str) -> tuple[dict[str, object], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise SkillValidationError("SKILL.md must start with YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise SkillValidationError("SKILL.md frontmatter is missing its closing ---") from exc
    try:
        raw = yaml.load("\n".join(lines[1:closing]), Loader=_UniqueKeyLoader)
    except SkillValidationError:
        raise
    except yaml.YAMLError as exc:
        raise SkillValidationError("Invalid SKILL.md YAML frontmatter") from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise SkillValidationError("Skill frontmatter must be a string-keyed YAML mapping")
    return raw, "\n".join(lines[closing + 1 :])


def _required_string(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise SkillValidationError(f"Skill frontmatter field {key} must be a string")
    return value


def _optional_string(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SkillValidationError(f"Skill frontmatter field {key} must be a non-empty string")
    return value.strip()


def _metadata(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise SkillValidationError("Skill metadata must map string keys to string values")
    return dict(value)


def _package_files(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise SkillValidationError(f"Skill package must not contain symlinks: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SkillValidationError(f"Skill package contains a non-regular file: {path}")
        size = path.stat().st_size
        if size > MAX_RESOURCE_BYTES and path.name != "SKILL.md":
            raise SkillValidationError(f"Skill resource exceeds {MAX_RESOURCE_BYTES} bytes: {path}")
        total += size
        paths.append(path)
    if len(paths) - 1 > MAX_RESOURCES:
        raise SkillValidationError(f"Skill cannot contain more than {MAX_RESOURCES} resources")
    if total > MAX_PACKAGE_BYTES:
        raise SkillValidationError(f"Skill package exceeds {MAX_PACKAGE_BYTES} bytes")
    return tuple(paths)


def _normalized_resource_path(value: str) -> str:
    candidate = PurePosixPath(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts or candidate == PurePosixPath("SKILL.md"):
        raise SkillValidationError(f"Skill resource must use a package-relative path: {value}")
    return candidate.as_posix()


def _resource_kind(path: Path) -> str:
    first = path.parts[0] if path.parts else ""
    return first if first in {"references", "assets", "scripts"} else "other"


def _digest(root: Path, paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
