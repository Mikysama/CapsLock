"""Shared helpers for direct-capability tools."""

from __future__ import annotations

from typing import Any

from ..contracts import ToolOutcome, ToolOutcomeStatus


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
        raise ValueError("path must be a string")
    return path


__all__ = []
