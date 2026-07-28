"""Configuration document loading."""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
from datetime import UTC, datetime

from .document import DocumentReader
from .validation import validate_config_document


def read_config_document(path: Path) -> dict[str, object]:
    return DocumentReader().read(path)


def load_config_document(path: Path) -> dict[str, object]:
    document = read_config_document(path)
    if document.get("config_version") in {3, 4, 5, 6, 7, 8}:
        _upgrade_config(path)
        document = read_config_document(path)
    errors = [
        item for item in validate_config_document(document) if item.severity == "error"
    ]
    if errors:
        first = errors[0]
        raise ValueError(f"invalid config at {first.path}: {first.message}")
    return document


def _upgrade_config(path: Path) -> None:
    """Backup and atomically upgrade a v3-v8 document without losing comments."""
    import tomlkit

    source = path.read_text(encoding="utf-8")
    document = tomlkit.parse(source)
    source_version = document.get("config_version")
    if source_version not in {3, 4, 5, 6, 7, 8}:
        return
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.v{source_version}-{timestamp}.bak")
    backup.write_text(source, encoding="utf-8")
    document["config_version"] = 9
    memory = document.setdefault("memory", {})
    if isinstance(memory, dict):
        old_enabled = bool(memory.pop("enabled", True))
        memory.setdefault("capture_enabled", old_enabled)
        memory.setdefault("recall_enabled", old_enabled)
        memory.setdefault("manual_write_enabled", old_enabled)
        memory.setdefault("maintenance_enabled", old_enabled)
        memory.setdefault("policy", "automatic" if old_enabled else "off")
    document.setdefault(
        "tools",
        {
            "schema_budget_tokens": 8000,
            "max_read_concurrency": 4,
            "aggregate_result_bytes": 65536,
        },
    )
    document.setdefault(
        "shell",
        {
            "enabled": True,
            "default_timeout_seconds": 120,
            "max_timeout_seconds": 600,
            "classifier_enabled": True,
            "classifier_threshold": 0.95,
            "background_enabled": True,
            "output_bytes": 100000,
        },
    )
    document.setdefault(
        "lsp",
        {
            "enabled": True,
            "startup_timeout_seconds": 10,
            "request_timeout_seconds": 15,
            "idle_timeout_seconds": 300,
        },
    )
    document.setdefault(
        "documents",
        {
            "max_pdf_bytes": 52428800,
            "max_pdf_pages": 10,
            "max_notebook_bytes": 10485760,
            "max_notebook_cells": 50,
            "max_cell_output_bytes": 65536,
        },
    )
    document.setdefault("worktree", {"enabled": True, "max_per_session": 4})
    agents = document.setdefault("agents", {})
    if isinstance(agents, dict):
        agents.setdefault("background_enabled", True)
        agents.setdefault("mailbox_enabled", True)
        agents.setdefault("message_ttl_seconds", 3600)
    context = document.setdefault("context", {})
    if isinstance(context, dict):
        context.setdefault("tokenizer", "adaptive")
    mcp = document.setdefault("mcp", {})
    if isinstance(mcp, dict):
        mcp.setdefault("remote_enabled", True)
    document.setdefault(
        "bridge",
        {"enabled": False, "max_selection_bytes": 65536, "max_diagnostics": 500},
    )
    document.setdefault(
        "observability",
        {"enabled": True, "retention_days": 30, "max_spans": 100000},
    )
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".config-v9-", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(tomlkit.dumps(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
