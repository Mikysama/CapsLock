"""Strict parsing and validation for data-only Skill packages."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from packaging.version import InvalidVersion, Version


MANIFEST_FIELDS = {
    "schema_version",
    "name",
    "version",
    "description",
    "min_capslock_version",
    "instructions",
    "input_schema",
    "output_schema",
    "required_tools",
    "required_permissions",
}
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INSTRUCTIONS_BYTES = 128 * 1024
MAX_SCHEMA_BYTES = 128 * 1024
MAX_FIXTURE_BYTES = 64 * 1024
MAX_FIXTURES = 100
KNOWN_PERMISSIONS = {
    "workspace.read",
    "workspace.write",
    "command.execute",
    "web.access",
    "mcp.call",
    "memory.read",
    "session.write",
}


class SkillValidationError(ValueError):
    """A deterministic, user-facing package validation failure."""


@dataclass(frozen=True)
class SkillManifest:
    schema_version: int
    name: str
    version: str
    description: str
    min_capslock_version: str
    instructions_path: str
    input_schema_path: str
    output_schema_path: str
    required_tools: tuple[str, ...]
    required_permissions: tuple[str, ...]


@dataclass(frozen=True)
class SkillPackage:
    root: Path
    scope: str
    manifest: SkillManifest
    instructions: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    digest: str

    @property
    def name(self) -> str:
        return self.manifest.name

    def validate_input(self, value: object) -> None:
        _validate_instance(self.input_schema, value, "Skill input")

    def validate_output(self, value: object) -> None:
        _validate_instance(self.output_schema, value, "Skill output")


def load_skill_package(
    root: Path,
    *,
    scope: str,
    current_version: str,
    available_tools: set[str],
) -> SkillPackage:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise SkillValidationError(f"Skill package must be a regular directory: {root}")
    manifest_path = _package_file(root, "skill.toml", MAX_MANIFEST_BYTES)
    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SkillValidationError(f"Invalid Skill manifest: {manifest_path}") from exc
    if not isinstance(raw, dict):
        raise SkillValidationError("Skill manifest must be a TOML table")
    unknown = sorted(set(raw) - MANIFEST_FIELDS)
    missing = sorted(MANIFEST_FIELDS - set(raw))
    if unknown:
        raise SkillValidationError(f"Unknown Skill manifest fields: {', '.join(unknown)}")
    if missing:
        raise SkillValidationError(f"Missing Skill manifest fields: {', '.join(missing)}")

    name = _string(raw, "name")
    if not NAME_PATTERN.fullmatch(name):
        raise SkillValidationError("Skill name must match [a-z][a-z0-9-]{0,63}")
    if root.name != name:
        raise SkillValidationError(f"Skill directory must match manifest name: {name}")
    version = _version(_string(raw, "version"), "version")
    minimum = _version(_string(raw, "min_capslock_version"), "min_capslock_version")
    try:
        compatible = Version(current_version) >= Version(minimum)
    except InvalidVersion as exc:
        raise SkillValidationError(f"Invalid CapsLock version: {current_version}") from exc
    if not compatible:
        raise SkillValidationError(
            f"Skill {name} requires CapsLock >= {minimum}; current version is {current_version}"
        )
    schema_version = raw["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise SkillValidationError("Unsupported Skill schema_version; expected 1")
    description = _string(raw, "description")
    if not description.strip():
        raise SkillValidationError("Skill description cannot be empty")
    required_tools = _strings(raw, "required_tools")
    required_permissions = _strings(raw, "required_permissions")
    unknown_permissions = sorted(set(required_permissions) - KNOWN_PERMISSIONS)
    if unknown_permissions:
        raise SkillValidationError(f"Unknown Skill permissions: {', '.join(unknown_permissions)}")
    _validate_tool_requirements(required_tools, available_tools)
    _validate_permission_requirements(required_tools, required_permissions)

    instructions_path = _string(raw, "instructions")
    input_path = _string(raw, "input_schema")
    output_path = _string(raw, "output_schema")
    instructions_file = _package_file(root, instructions_path, MAX_INSTRUCTIONS_BYTES)
    input_file = _package_file(root, input_path, MAX_SCHEMA_BYTES)
    output_file = _package_file(root, output_path, MAX_SCHEMA_BYTES)
    try:
        instructions = instructions_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillValidationError(f"Skill instructions must be UTF-8: {instructions_path}") from exc
    if not instructions.strip():
        raise SkillValidationError("Skill instructions cannot be empty")
    input_schema = _load_schema(input_file, "input")
    output_schema = _load_schema(output_file, "output")
    fixture_files = _fixture_files(root)
    digest = _digest((manifest_path, instructions_file, input_file, output_file, *fixture_files))
    package = SkillPackage(
        root=root,
        scope=scope,
        manifest=SkillManifest(
            schema_version=1,
            name=name,
            version=version,
            description=description.strip(),
            min_capslock_version=minimum,
            instructions_path=instructions_path,
            input_schema_path=input_path,
            output_schema_path=output_path,
            required_tools=required_tools,
            required_permissions=required_permissions,
        ),
        instructions=instructions,
        input_schema=input_schema,
        output_schema=output_schema,
        digest=digest,
    )
    _validate_fixtures(package, fixture_files)
    return package


def base_tool_name(requirement: str) -> str:
    if requirement.startswith("command:"):
        return "propose_command"
    if requirement.startswith("mcp:"):
        return "propose_mcp_call"
    return requirement


def _validate_tool_requirements(requirements: tuple[str, ...], available: set[str]) -> None:
    for item in requirements:
        if not item or item.strip() != item:
            raise SkillValidationError("Skill tool requirements must be non-empty normalized strings")
        if item.startswith("command:"):
            if item.count(":") != 1 or not item.partition(":")[2]:
                raise SkillValidationError(f"Invalid command tool requirement: {item}")
        elif item.startswith("mcp:"):
            parts = item.split(":")
            if len(parts) != 3 or not all(parts[1:]):
                raise SkillValidationError(f"Invalid MCP tool requirement: {item}")
        elif ":" in item:
            raise SkillValidationError(f"Unsupported qualified tool requirement: {item}")
        base = base_tool_name(item)
        if base not in available:
            raise SkillValidationError(f"Skill requires unavailable tool: {item}")


def _validate_permission_requirements(
    tools: tuple[str, ...], permissions: tuple[str, ...]
) -> None:
    mapping = {
        "list_files": "workspace.read",
        "read_file": "workspace.read",
        "search_files": "workspace.read",
        "git_status": "workspace.read",
        "git_diff": "workspace.read",
        "propose_file_edit": "workspace.write",
        "propose_file_create": "workspace.write",
        "apply_change": "workspace.write",
        "discard_change": "workspace.write",
        "propose_command": "command.execute",
        "run_command": "command.execute",
        "discard_command": "command.execute",
        "propose_web_search": "web.access",
        "propose_web_fetch": "web.access",
        "propose_mcp_connect": "mcp.call",
        "propose_mcp_call": "mcp.call",
        "search_memories": "memory.read",
        "get_memory": "memory.read",
        "task_list_update": "session.write",
        "task_status_update": "session.write",
        "list_external_sources": "session.write",
    }
    inferred = {mapping[base_tool_name(item)] for item in tools if base_tool_name(item) in mapping}
    missing = sorted(inferred - set(permissions))
    if missing:
        raise SkillValidationError(
            f"Skill required_permissions is missing: {', '.join(missing)}"
        )


def _load_schema(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillValidationError(f"Invalid {label} JSON Schema: {path.name}") from exc
    if not isinstance(raw, dict):
        raise SkillValidationError(f"Skill {label} schema must be an object")
    if raw.get("type") != "object":
        raise SkillValidationError(f"Skill {label} schema root type must be object")
    _reject_external_refs(raw)
    try:
        Draft202012Validator.check_schema(raw)
    except SchemaError as exc:
        raise SkillValidationError(f"Invalid {label} JSON Schema: {exc.message}") from exc
    return raw


def _fixture_files(root: Path) -> tuple[Path, ...]:
    directory = root / "fixtures"
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise SkillValidationError("Skill fixtures must be a regular directory")
    paths = sorted(directory.iterdir(), key=lambda item: item.name)
    if len(paths) > MAX_FIXTURES:
        raise SkillValidationError(f"Skill cannot contain more than {MAX_FIXTURES} fixtures")
    output: list[Path] = []
    for path in paths:
        if path.suffix != ".json":
            raise SkillValidationError(f"Skill fixture must use .json: {path.name}")
        output.append(_package_file(root, f"fixtures/{path.name}", MAX_FIXTURE_BYTES))
    return tuple(output)


def _validate_fixtures(package: SkillPackage, paths: tuple[Path, ...]) -> None:
    allowed = {"input", "output", "input_valid", "output_valid"}
    for fixture_file in paths:
        try:
            raw = json.loads(fixture_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SkillValidationError(f"Invalid Skill fixture: {fixture_file.name}") from exc
        if not isinstance(raw, dict) or not raw or set(raw) - allowed:
            raise SkillValidationError(f"Invalid Skill fixture fields: {fixture_file.name}")
        for key, schema, flag in (
            ("input", package.input_schema, "input_valid"),
            ("output", package.output_schema, "output_valid"),
        ):
            if key not in raw:
                continue
            expected = raw.get(flag, True)
            if not isinstance(expected, bool):
                raise SkillValidationError(
                    f"Skill fixture {flag} must be boolean: {fixture_file.name}"
                )
            valid = not list(Draft202012Validator(schema).iter_errors(raw[key]))
            if valid != expected:
                state = "valid" if expected else "invalid"
                raise SkillValidationError(
                    f"Skill fixture expected {key} to be {state}: {fixture_file.name}"
                )


def _reject_external_refs(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                raise SkillValidationError("Skill schemas may only use internal $ref values")
            _reject_external_refs(item)
    elif isinstance(value, list):
        for item in value:
            _reject_external_refs(item)


def _validate_instance(schema: dict[str, Any], value: object, label: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        error: ValidationError = errors[0]
        location = ".".join(str(item) for item in error.absolute_path)
        suffix = f" at {location}" if location else ""
        raise SkillValidationError(
            f"{label} is invalid{suffix}: failed {error.validator!r} constraint"
        )


def _package_file(root: Path, relative: str, limit: int) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not relative or ".." in candidate.parts:
        raise SkillValidationError(f"Skill file must use a package-relative path: {relative}")
    path = root.joinpath(candidate)
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise SkillValidationError(f"Skill file path must not contain symlinks: {relative}")
    if path.is_symlink() or not path.is_file():
        raise SkillValidationError(f"Skill file must be a regular file: {relative}")
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise SkillValidationError(f"Skill file escapes its package: {relative}") from exc
    if path.stat().st_size > limit:
        raise SkillValidationError(f"Skill file exceeds {limit} bytes: {relative}")
    return path


def _string(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise SkillValidationError(f"Skill manifest field {key} must be a string")
    return value


def _strings(raw: dict[str, object], key: str) -> tuple[str, ...]:
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SkillValidationError(f"Skill manifest field {key} must be an array of strings")
    if len(value) != len(set(value)):
        raise SkillValidationError(f"Skill manifest field {key} contains duplicates")
    return tuple(value)


def _version(value: str, field: str) -> str:
    try:
        parsed = Version(value)
    except InvalidVersion as exc:
        raise SkillValidationError(f"Skill manifest field {field} must be a valid version") from exc
    return str(parsed)


def _digest(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
