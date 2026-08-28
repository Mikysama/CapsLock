"""Public configuration facade over focused implementation modules."""

import os
import tempfile
from pathlib import Path

from ..behavior_defaults import DEFAULT_MAX_TOOL_ROUNDS
from .loader import load_config_document, read_config_document
from .settings import Settings
from .types import (
    AgentSettings,
    BridgeSettings,
    BudgetSettings,
    CommandSettings,
    ConfigIssue,
    ContextSettings,
    DocumentSettings,
    LspServerSettings,
    LspSettings,
    McpSettings,
    MemorySettings,
    ModelProfileSettings,
    ModelSettings,
    ObservabilitySettings,
    ProviderSettings,
    RoutingSettings,
    RuntimeSettings,
    ShellSettings,
    ToolSettings,
    WebSettings,
    WorktreeSettings,
)
from .validation import CONFIG_VERSION, validate_config_document

__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_MAX_TOOL_ROUNDS",
    "AgentSettings",
    "BridgeSettings",
    "BudgetSettings",
    "CommandSettings",
    "ConfigIssue",
    "ContextSettings",
    "DocumentSettings",
    "LspServerSettings",
    "LspSettings",
    "McpSettings",
    "MemorySettings",
    "ModelProfileSettings",
    "ModelSettings",
    "ObservabilitySettings",
    "ProviderSettings",
    "RoutingSettings",
    "RuntimeSettings",
    "Settings",
    "ShellSettings",
    "ToolSettings",
    "WebSettings",
    "WorktreeSettings",
    "load_config_document",
    "read_config_document",
    "validate_config_document",
    "write_config",
]


def write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".config-", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
