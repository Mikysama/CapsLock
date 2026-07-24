"""Local plugin installation and workspace authorization lifecycle."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .. import __version__
from ..layout import ProjectLayout
from .client import PluginProcessClient
from .manifest import PluginManifest, PluginValidationError, load_plugin_manifest
from .registry import InstalledPlugin, PluginRegistry, append_plugin_audit
from .sandbox import SandboxUnavailableError


class PluginService:
    def __init__(
        self,
        layout: ProjectLayout,
        *,
        client: PluginProcessClient | None = None,
    ) -> None:
        self.layout = layout
        self.registry = PluginRegistry(layout)
        self.client = client or PluginProcessClient()

    def entries(self) -> list[InstalledPlugin]:
        return self.registry.entries()

    async def install(self, source: Path) -> PluginManifest:
        source = source.expanduser().resolve()
        manifest = load_plugin_manifest(source)
        _check_capslock_compatibility(manifest)
        target = self.layout.user.plugins / manifest.name / manifest.version
        if target.exists():
            installed = load_plugin_manifest(target)
            if installed.digest != manifest.digest:
                raise PluginValidationError(
                    f"plugin version already exists with different contents: {manifest.name} {manifest.version}"
                )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{manifest.version}.", dir=target.parent)
            )
            try:
                shutil.copytree(source, temporary, dirs_exist_ok=True, symlinks=False)
                copied = load_plugin_manifest(temporary)
                if copied.digest != manifest.digest:
                    raise PluginValidationError(
                        "plugin changed while it was being installed"
                    )
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        installed = load_plugin_manifest(target)
        previous = next(
            (
                entry.manifest
                for entry in self.entries()
                if entry.manifest.name == manifest.name
            ),
            None,
        )
        self.registry.write_install(installed, source)
        append_plugin_audit(
            self.layout.user,
            {
                "operation": "upgrade" if previous else "install",
                "plugin": installed.name,
                "version": installed.version,
                "digest": installed.digest,
                "previous_version": previous.version if previous else None,
                "previous_digest": previous.digest if previous else None,
                "permissions": sorted(item.value for item in installed.permissions),
                "workspace": str(self.layout.workspace),
                "result": "completed",
            },
        )
        return installed

    async def verify(self, name: str) -> PluginManifest:
        entry = self.registry.get(name, require_enabled=False)
        await self.client.verify(
            entry.manifest, trusted_native=entry.trusted_native
        )
        return entry.manifest

    def enable(
        self,
        name: str,
        *,
        trusted_native: bool = False,
        allow_session_lifecycle: bool = False,
    ) -> PluginManifest:
        entry = self.registry.get(name, require_enabled=False)
        if entry.manifest.lifecycle == "session" and not allow_session_lifecycle:
            raise PluginValidationError(
                "session-lifecycle plugins require separate --session-lifecycle authorization"
            )
        if self.client.sandbox is None and not trusted_native:
            raise SandboxUnavailableError(
                "no plugin sandbox backend is available; use --trusted-native explicitly"
            )
        self.registry.enable(
            entry.manifest,
            trusted_native=trusted_native,
            allow_session_lifecycle=allow_session_lifecycle,
        )
        append_plugin_audit(
            self.layout.user,
            {
                "operation": "enable",
                "plugin": name,
                "version": entry.manifest.version,
                "digest": entry.manifest.digest,
                "permissions": sorted(
                    item.value for item in entry.manifest.permissions
                ),
                "capabilities": entry.manifest.capabilities.as_dict(),
                "mode": "trusted-native" if trusted_native else "sandboxed",
                "lifecycle": entry.manifest.lifecycle,
                "workspace": str(self.layout.workspace),
                "result": "completed",
            },
        )
        return entry.manifest

    def disable(self, name: str) -> PluginManifest:
        entry = self.registry.get(name, require_enabled=False)
        self.registry.disable(name)
        append_plugin_audit(
            self.layout.user,
            {
                "operation": "disable",
                "plugin": name,
                "version": entry.manifest.version,
                "digest": entry.manifest.digest,
                "workspace": str(self.layout.workspace),
                "result": "completed",
            },
        )
        return entry.manifest

    def uninstall(self, name: str) -> PluginManifest:
        entry = self.registry.get(name, require_enabled=False)
        workspaces = self.registry.enabled_workspaces(name)
        if workspaces:
            raise PluginValidationError(
                "plugin is still enabled in: " + ", ".join(workspaces)
            )
        self.registry.remove_install(name)
        plugin_root = self.layout.user.plugins / name
        if plugin_root.exists():
            shutil.rmtree(plugin_root)
        append_plugin_audit(
            self.layout.user,
            {
                "operation": "uninstall",
                "plugin": name,
                "version": entry.manifest.version,
                "digest": entry.manifest.digest,
                "workspace": str(self.layout.workspace),
                "result": "completed",
            },
        )
        return entry.manifest


def _check_capslock_compatibility(manifest: PluginManifest) -> None:
    requirement = manifest.requires_capslock
    if not requirement:
        return
    current = _version_tuple(__version__)
    for clause in requirement.split(","):
        clause = clause.strip()
        operator = next(
            (item for item in (">=", "<=", "==", ">", "<") if clause.startswith(item)),
            None,
        )
        if operator is None:
            raise PluginValidationError(
                "requires_capslock must use comma-separated version comparisons"
            )
        expected = _version_tuple(clause[len(operator) :].strip())
        valid = {
            ">=": current >= expected,
            "<=": current <= expected,
            "==": current == expected,
            ">": current > expected,
            "<": current < expected,
        }[operator]
        if not valid:
            raise PluginValidationError(
                f"plugin requires CapsLock {requirement}; current version is {__version__}"
            )


def _version_tuple(value: str) -> tuple[int, int, int]:
    try:
        core = value.split("-", 1)[0].split("+", 1)[0]
        major, minor, patch = core.split(".")
        return int(major), int(minor), int(patch)
    except (TypeError, ValueError) as exc:
        raise PluginValidationError(f"invalid version comparison: {value}") from exc
