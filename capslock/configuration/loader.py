"""Configuration document loading and migration orchestration."""

from __future__ import annotations

from pathlib import Path

from .document import DocumentReader
from .migration import migrate_config
from .validation import CONFIG_VERSION, validate_config_document


def read_config_document(path: Path) -> dict[str, object]:
    return DocumentReader().read(path)


def load_config_document(path: Path, *, migrate: bool = False) -> dict[str, object]:
    document = read_config_document(path)
    version = document.get("config_version", 0)
    if isinstance(version, int) and version < CONFIG_VERSION and migrate:
        migrate_config(path, apply=True)
        document = read_config_document(path)
    errors = [
        item for item in validate_config_document(document) if item.severity == "error"
    ]
    if errors:
        first = errors[0]
        raise ValueError(f"invalid config at {first.path}: {first.message}")
    return document
