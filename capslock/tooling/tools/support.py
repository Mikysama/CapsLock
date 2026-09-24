"""Shared helpers for direct-capability tools."""

from __future__ import annotations

from typing import Any
import re

from ..contracts import ToolOutcome, ToolOutcomeStatus
from ...policy import InvalidPathError


def _outcome(
    ok: bool, data: object, error: str | None = None, **values: Any
) -> ToolOutcome:
    """Internal adapter helper while built-ins use the v2 outcome envelope."""
    return ToolOutcome(
        ToolOutcomeStatus.SUCCEEDED if ok else ToolOutcomeStatus.FAILED,
        ok,
        data=data,
        error=error,
        error_code=None if ok else "tool_failed",
        **values,
    )


def _path(arguments: dict[str, Any]) -> str:
    path = arguments.get("path", ".")
    if not isinstance(path, str):
        raise InvalidPathError("path must be a string")
    candidate = path.strip()
    if not candidate:
        raise InvalidPathError("path must be a non-empty workspace-relative path")
    # Models occasionally emit source fragments (decorators, calls, or
    # expressions) in the path slot. Reject these before filesystem resolution
    # so the normal argument-repair loop can ask for a real repository path.
    if (
        "\n" in candidate
        or "\r" in candidate
        or candidate.startswith("@")
        or re.match(r"^[A-Za-z_][A-Za-z0-9_.]*\s*\(", candidate)
        or re.match(
            r"^(pytest\.mark\.|classmethod$|staticmethod$|override_settings$)",
            candidate,
        )
    ):
        raise InvalidPathError(
            f"invalid path expression: {candidate!r}; use a repository-relative file path"
        )
    return candidate


__all__ = ["InvalidPathError"]
