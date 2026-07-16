"""Read-only model tools for progressive Skill disclosure."""

from __future__ import annotations

from typing import Any

from .core import RunContext, ToolResult


def load_skill(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    if context.skills is None:
        raise ValueError("Skill loading is unavailable")
    data, audit = context.skills.load_data(context.run_id, name, trigger="model")
    return ToolResult(True, data, audit_data=audit)


def read_skill_resource(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    name, path = arguments.get("name"), arguments.get("path")
    start_line, end_line = arguments.get("start_line", 1), arguments.get("end_line")
    if not isinstance(name, str) or not isinstance(path, str):
        raise ValueError("name and path must be strings")
    if not isinstance(start_line, int) or end_line is not None and not isinstance(end_line, int):
        raise ValueError("start_line and end_line must be integers")
    if context.skills is None:
        raise ValueError("Skill loading is unavailable")
    data, audit = context.skills.read_resource(
        context.run_id,
        name,
        path,
        start_line=start_line,
        end_line=end_line,
    )
    return ToolResult(True, data, audit_data=audit)
