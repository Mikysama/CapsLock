"""Pure TOML document reading."""

from __future__ import annotations

import tomllib
from pathlib import Path


class DocumentReader:
    def read(self, path: Path) -> dict[str, object]:
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"invalid project configuration: {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise ValueError("project configuration must be a TOML document")
        return document
