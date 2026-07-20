"""Pure text diff helper used by file action proposals."""

from __future__ import annotations

import difflib
from pathlib import Path


def make_diff(path: Path, before: str | None, after: str) -> str:
    before_lines = [] if before is None else before.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
