"""Configuration document loading."""

from __future__ import annotations

from pathlib import Path

from .document import DocumentReader
from .validation import validate_config_document


def read_config_document(path: Path) -> dict[str, object]:
    return DocumentReader().read(path)


def load_config_document(path: Path) -> dict[str, object]:
    document = read_config_document(path)
    errors = [
        item for item in validate_config_document(document) if item.severity == "error"
    ]
    if errors:
        first = errors[0]
        raise ValueError(f"invalid config at {first.path}: {first.message}")
    return document
