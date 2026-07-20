"""Shared security-sensitive constants and redaction helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


TEXT_SUFFIXES = {
    ".c",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".json",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password)")
_SECRET_TEXT = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|password)\s*[=:]\s*[^\s,]+"
)
_BEARER_TEXT = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]+=*")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_KNOWN_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b"
)


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
        safe = _PRIVATE_KEY.sub("<redacted>", value)
        safe = _BEARER_TEXT.sub(r"\1 <redacted>", safe)
        safe = _KNOWN_TOKEN.sub("<redacted>", safe)
        return _SECRET_TEXT.sub(r"\1=<redacted>", safe)
    return value


def sanitize_memory_text(value: str) -> tuple[str, tuple[str, ...]]:
    """Replace secret-like memory content and report rule names without exposing values."""
    rules: list[str] = []

    def replace(
        pattern: re.Pattern[str], replacement: str, name: str, text: str
    ) -> str:
        if pattern.search(text):
            rules.append(name)
            return pattern.sub(replacement, text)
        return text

    safe = replace(_PRIVATE_KEY, "<redacted>", "private_key", value)
    safe = replace(_BEARER_TEXT, r"\1 <redacted>", "bearer_token", safe)
    safe = replace(_KNOWN_TOKEN, "<redacted>", "known_token", safe)
    safe = replace(_SECRET_TEXT, r"\1=<redacted>", "secret_field", safe)
    return safe, tuple(dict.fromkeys(rules))
