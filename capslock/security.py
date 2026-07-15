"""Shared security-sensitive constants and redaction helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


TEXT_SUFFIXES = {
    ".c", ".cpp", ".css", ".csv", ".go", ".h", ".html", ".java",
    ".js", ".json", ".md", ".py", ".rs", ".sh", ".sql", ".toml",
    ".ts", ".tsx", ".txt", ".yaml", ".yml",
}

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password)")
_SECRET_TEXT = re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password)\s*[=:]\s*[^\s,]+")


def redact(value: Any) -> Any:
    """Recursively redact values stored under secret-looking keys."""
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1=<redacted>", value)
    return value
