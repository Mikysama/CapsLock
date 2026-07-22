"""Versioned local plugin manifest parsing and package validation."""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_NAME = "capslock-plugin.toml"
MANIFEST_VERSION = 1
PROTOCOL_VERSION = 1
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOOL_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_FILES = 1_000
MAX_FILE_BYTES = 5 * 1024 * 1024


class PluginValidationError(ValueError):
    """A deterministic, user-facing plugin validation failure."""


class PluginPermission(StrEnum):
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    NETWORK = "network"
    PROCESS = "process"
    CREDENTIALS = "credentials"


@dataclass(frozen=True)
class PluginToolSpec:
    name: str
    description: str
    parameters: dict[str, object]

    def qualified_name(self, plugin_name: str) -> str:
        return f"plugin.{plugin_name}.{self.name}"


@dataclass(frozen=True)
class PluginManifestV1:
    root: Path
    name: str
    version: str
    description: str
    entrypoint: tuple[str, ...]
    tools: tuple[PluginToolSpec, ...]
    permissions: frozenset[PluginPermission]
    digest: str
    requires_capslock: str | None = None
    manifest_version: int = MANIFEST_VERSION
    protocol_version: int = PROTOCOL_VERSION


def load_plugin_manifest(root: Path) -> PluginManifestV1:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise PluginValidationError(
            f"plugin package must be a regular directory: {root}"
        )
    files = _package_files(root)
    manifest_path = root / MANIFEST_NAME
    if manifest_path not in files:
        raise PluginValidationError(f"plugin package requires {MANIFEST_NAME}: {root}")
    try:
        document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PluginValidationError(
            f"invalid plugin manifest: {manifest_path}"
        ) from exc
    allowed = {
        "manifest_version",
        "protocol_version",
        "name",
        "version",
        "description",
        "requires_capslock",
        "entrypoint",
        "permissions",
        "tools",
    }
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise PluginValidationError(
            f"unsupported plugin manifest fields: {', '.join(unknown)}"
        )
    manifest_version = _integer(document, "manifest_version")
    protocol_version = _integer(document, "protocol_version")
    if manifest_version != MANIFEST_VERSION:
        raise PluginValidationError(
            f"unsupported plugin manifest version: {manifest_version}"
        )
    if protocol_version != PROTOCOL_VERSION:
        raise PluginValidationError(
            f"unsupported plugin protocol version: {protocol_version}"
        )
    name = _string(document, "name")
    if not NAME_PATTERN.fullmatch(name) or len(name) > 64:
        raise PluginValidationError(
            "plugin name must use lowercase hyphen-separated words"
        )
    version = _string(document, "version")
    if not re.fullmatch(
        r"0|[1-9]\d*(?:\.(?:0|[1-9]\d*)){2}(?:[-+][0-9A-Za-z.-]+)?", version
    ):
        raise PluginValidationError("plugin version must be a semantic version")
    description = _string(document, "description").strip()
    if not description or len(description) > 1024:
        raise PluginValidationError("plugin description must contain 1-1024 characters")
    entrypoint_raw = document.get("entrypoint")
    if (
        not isinstance(entrypoint_raw, list)
        or not entrypoint_raw
        or not all(isinstance(item, str) and item for item in entrypoint_raw)
    ):
        raise PluginValidationError(
            "plugin entrypoint must be a non-empty string array"
        )
    entrypoint = tuple(entrypoint_raw)
    if (
        entrypoint[0].startswith(("/", "~"))
        or ".." in PurePosixPath(entrypoint[0]).parts
    ):
        raise PluginValidationError(
            "plugin entrypoint executable must be package-relative"
        )
    executable = root / entrypoint[0]
    if executable.is_symlink() or not executable.is_file():
        raise PluginValidationError(
            f"plugin entrypoint does not exist: {entrypoint[0]}"
        )
    permissions_raw = document.get("permissions", [])
    if not isinstance(permissions_raw, list) or not all(
        isinstance(item, str) for item in permissions_raw
    ):
        raise PluginValidationError("plugin permissions must be a string array")
    try:
        permissions = frozenset(PluginPermission(item) for item in permissions_raw)
    except ValueError as exc:
        raise PluginValidationError(
            f"unsupported plugin permission: {exc.args[0]}"
        ) from exc
    tools_raw = document.get("tools")
    if not isinstance(tools_raw, list) or not tools_raw:
        raise PluginValidationError("plugin must declare at least one tool")
    tools: list[PluginToolSpec] = []
    names: set[str] = set()
    for raw in tools_raw:
        if not isinstance(raw, dict) or set(raw) != {
            "name",
            "description",
            "parameters",
        }:
            raise PluginValidationError(
                "each plugin tool requires name, description, and parameters"
            )
        tool_name = _string(raw, "name")
        if not TOOL_PATTERN.fullmatch(tool_name) or len(tool_name) > 64:
            raise PluginValidationError(f"invalid plugin tool name: {tool_name}")
        if tool_name in names:
            raise PluginValidationError(f"duplicate plugin tool name: {tool_name}")
        names.add(tool_name)
        tool_description = _string(raw, "description").strip()
        parameters = raw.get("parameters")
        if (
            not tool_description
            or not isinstance(parameters, dict)
            or parameters.get("type") != "object"
        ):
            raise PluginValidationError(f"invalid plugin tool declaration: {tool_name}")
        tools.append(PluginToolSpec(tool_name, tool_description, parameters))
    return PluginManifestV1(
        root=root,
        name=name,
        version=version,
        description=description,
        entrypoint=entrypoint,
        tools=tuple(tools),
        permissions=permissions,
        digest=_digest(root, files),
        requires_capslock=_optional_string(document, "requires_capslock"),
        manifest_version=manifest_version,
        protocol_version=protocol_version,
    )


def _package_files(root: Path) -> list[Path]:
    files: list[Path] = []
    total = 0
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            if (current_path / directory).is_symlink():
                raise PluginValidationError(
                    "plugin packages cannot contain symbolic links"
                )
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise PluginValidationError(
                    "plugin packages may contain regular files only"
                )
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise PluginValidationError(
                    f"plugin file exceeds {MAX_FILE_BYTES} bytes: {path}"
                )
            total += size
            files.append(path)
            if len(files) > MAX_FILES or total > MAX_PACKAGE_BYTES:
                raise PluginValidationError(
                    "plugin package exceeds size or file-count limits"
                )
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _digest(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise PluginValidationError(
            f"plugin manifest field {key} must be a non-empty string"
        )
    return value


def _optional_string(document: dict[str, Any], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PluginValidationError(
            f"plugin manifest field {key} must be a non-empty string"
        )
    return value


def _integer(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PluginValidationError(f"plugin manifest field {key} must be an integer")
    return value
