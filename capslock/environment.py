"""Small dependency-free loader for project environment files."""

from __future__ import annotations

import os
from pathlib import Path


def load_project_environment(directory: str | Path = ".") -> None:
    """Load .env without replacing shell-provided variables."""
    root = Path(directory)
    shell_variables = set(os.environ)
    path = root / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), _unquote(value.strip())
        if name and name not in shell_variables:
            os.environ[name] = value


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
        return value[1:-1]
    return value
