"""Atomic user installation registry and workspace plugin grants."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..layout import ProjectLayout, UserLayout
from .manifest import (
    PluginCapabilities,
    PluginManifest,
    PluginPermission,
    PluginValidationError,
    load_plugin_manifest,
)


REGISTRY_VERSION = 3


@dataclass(frozen=True)
class InstalledPlugin:
    manifest: PluginManifest
    enabled: bool
    granted_capabilities: PluginCapabilities
    trusted_native: bool = False

    @property
    def granted_permissions(self) -> frozenset[PluginPermission]:
        return self.granted_capabilities.permissions


class PluginRegistry:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout

    def entries(self) -> list[InstalledPlugin]:
        registry = self._read(self.layout.user.plugin_registry)
        grants = self._read(self.layout.local_plugins)
        enabled = grants.get("plugins", {})
        entries: list[InstalledPlugin] = []
        for name, record in sorted(registry.get("plugins", {}).items()):
            if not isinstance(record, dict):
                continue
            path = self.layout.user.plugins / name / str(record.get("version", ""))
            try:
                manifest = load_plugin_manifest(path)
            except PluginValidationError:
                continue
            grant = enabled.get(name, {}) if isinstance(enabled, dict) else {}
            capabilities = _capabilities_from_record(
                grant.get("capabilities", {}) if isinstance(grant, dict) else {}
            )
            authorized = (
                isinstance(grant, dict)
                and grant.get("version") == manifest.version
                and grant.get("digest") == manifest.digest
                and manifest.capabilities.contains(capabilities)
                and (
                    manifest.lifecycle == "invocation"
                    or grant.get("session_lifecycle_authorized") is True
                )
            )
            entries.append(
                InstalledPlugin(
                    manifest,
                    authorized,
                    capabilities,
                    bool(grant.get("trusted_native", False))
                    if isinstance(grant, dict)
                    else False,
                )
            )
        return entries

    def validate(self) -> list[str]:
        errors: list[str] = []
        registry = self._read(self.layout.user.plugin_registry)
        grants = self._read(self.layout.local_plugins)
        installed = registry.get("plugins", {})
        for name, record in sorted(installed.items()):
            if not isinstance(record, dict):
                errors.append(f"{name}: invalid installation record")
                continue
            path = self.layout.user.plugins / name / str(record.get("version", ""))
            try:
                manifest = load_plugin_manifest(path)
                if manifest.name != name:
                    errors.append(f"{name}: installed package identity mismatch")
                if record.get("digest") != manifest.digest:
                    errors.append(f"{name}: installed package digest mismatch")
            except PluginValidationError as exc:
                errors.append(f"{name}: {exc}")
        for name, grant in sorted(grants.get("plugins", {}).items()):
            record = installed.get(name)
            if not isinstance(grant, dict) or not isinstance(record, dict):
                errors.append(f"{name}: workspace grant references a missing plugin")
                continue
            if grant.get("version") != record.get("version") or grant.get(
                "digest"
            ) != record.get("digest"):
                errors.append(
                    f"{name}: workspace grant does not match installed package"
                )
        return errors

    def get(self, name: str, *, require_enabled: bool = True) -> InstalledPlugin:
        entry = next(
            (item for item in self.entries() if item.manifest.name == name), None
        )
        if entry is None:
            raise PluginValidationError(f"plugin is not installed: {name}")
        if require_enabled and not entry.enabled:
            raise PluginValidationError(
                f"plugin is not enabled in this workspace: {name}"
            )
        current = load_plugin_manifest(entry.manifest.root)
        if current.digest != entry.manifest.digest:
            raise PluginValidationError(f"plugin package changed on disk: {name}")
        return entry

    def write_install(self, manifest: PluginManifest, source: Path) -> None:
        document = self._read(self.layout.user.plugin_registry)
        plugins = document.setdefault("plugins", {})
        previous = plugins.get(manifest.name, {})
        workspaces = (
            previous.get("workspaces", []) if isinstance(previous, dict) else []
        )
        plugins[manifest.name] = {
            "version": manifest.version,
            "digest": manifest.digest,
            "source": str(source),
            "installed_at": _now(),
            "workspaces": workspaces,
        }
        self._write(self.layout.user.plugin_registry, document)

    def remove_install(self, name: str) -> None:
        document = self._read(self.layout.user.plugin_registry)
        document.setdefault("plugins", {}).pop(name, None)
        self._write(self.layout.user.plugin_registry, document)

    def enable(
        self,
        manifest: PluginManifest,
        *,
        capabilities: PluginCapabilities | None = None,
        trusted_native: bool = False,
        allow_session_lifecycle: bool = False,
    ) -> None:
        granted = capabilities or manifest.capabilities
        if not manifest.capabilities.contains(granted):
            raise PluginValidationError(
                "workspace grant cannot expand manifest capabilities"
            )
        document = self._read(self.layout.local_plugins)
        document.setdefault("plugins", {})[manifest.name] = {
            "version": manifest.version,
            "digest": manifest.digest,
            "capabilities": granted.as_dict(),
            "trusted_native": trusted_native,
            "session_lifecycle_authorized": allow_session_lifecycle,
            "enabled_at": _now(),
        }
        self._write(self.layout.local_plugins, document, mode=0o600)
        registry = self._read(self.layout.user.plugin_registry)
        record = registry.setdefault("plugins", {}).get(manifest.name)
        if isinstance(record, dict):
            workspaces = set(record.get("workspaces", []))
            workspaces.add(str(self.layout.workspace))
            record["workspaces"] = sorted(workspaces)
            self._write(self.layout.user.plugin_registry, registry)

    def disable(self, name: str) -> None:
        document = self._read(self.layout.local_plugins)
        document.setdefault("plugins", {}).pop(name, None)
        self._write(self.layout.local_plugins, document, mode=0o600)
        registry = self._read(self.layout.user.plugin_registry)
        record = registry.setdefault("plugins", {}).get(name)
        if isinstance(record, dict):
            record["workspaces"] = [
                item
                for item in record.get("workspaces", [])
                if item != str(self.layout.workspace)
            ]
            self._write(self.layout.user.plugin_registry, registry)

    def enabled_workspaces(self, name: str) -> tuple[str, ...]:
        record = (
            self._read(self.layout.user.plugin_registry).get("plugins", {}).get(name)
        )
        if not isinstance(record, dict):
            return ()
        return tuple(
            item for item in record.get("workspaces", []) if isinstance(item, str)
        )

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"version": REGISTRY_VERSION, "plugins": {}}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PluginValidationError(f"invalid plugin registry: {path}") from exc
        if (
            not isinstance(document, dict)
            or document.get("version") != REGISTRY_VERSION
        ):
            raise PluginValidationError(f"unsupported plugin registry: {path}")
        if not isinstance(document.get("plugins"), dict):
            raise PluginValidationError(f"invalid plugin registry: {path}")
        return document

    @staticmethod
    def _write(path: Path, document: dict[str, Any], *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    document, stream, ensure_ascii=False, indent=2, sort_keys=True
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def append_plugin_audit(user: UserLayout, event: dict[str, object]) -> None:
    path = user.plugin_audit
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": _now(), **event}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _capabilities_from_record(value: object) -> PluginCapabilities:
    if not isinstance(value, dict):
        return PluginCapabilities()

    def items(name: str) -> tuple[str, ...]:
        raw = value.get(name, [])
        return tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()

    return PluginCapabilities(
        items("workspace_read"),
        items("workspace_write"),
        items("network_hosts"),
        items("process_templates"),
        items("credentials"),
    )
