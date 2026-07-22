"""Local tool plugin contracts and lifecycle services."""

from .manifest import (
    MANIFEST_VERSION,
    PROTOCOL_VERSION,
    PluginManifestV1,
    PluginPermission,
    PluginToolSpec,
    PluginValidationError,
    load_plugin_manifest,
)
from .client import PluginProcessClient, PluginProtocolError
from .registry import InstalledPlugin, PluginRegistry, append_plugin_audit
from .service import PluginService

__all__ = [
    "InstalledPlugin",
    "MANIFEST_VERSION",
    "PROTOCOL_VERSION",
    "PluginManifestV1",
    "PluginPermission",
    "PluginProcessClient",
    "PluginProtocolError",
    "PluginRegistry",
    "PluginService",
    "PluginToolSpec",
    "PluginValidationError",
    "append_plugin_audit",
    "load_plugin_manifest",
]
