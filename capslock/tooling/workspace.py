"""Read-only workspace and Git tools."""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import Any

from ..evidence import Evidence
from ..security import TEXT_SUFFIXES
from .core import RunContext, ToolResult


def _path(arguments: dict[str, Any]) -> str:
    path = arguments.get("path", ".")
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    return path


def _evidence(path: Path, query: str, context_lines: int = 2, limit: int = 8) -> list[Evidence]:
    terms = [item.casefold() for item in query.split() if item.strip()]
    lines = path.read_text(encoding="utf-8").splitlines()
    found: list[Evidence] = []
    for index, line in enumerate(lines):
        lower = line.casefold()
        if query.casefold() in lower or any(term in lower for term in terms):
            start, end = max(0, index - context_lines), min(len(lines), index + context_lines + 1)
            found.append(Evidence(path, start + 1, end, "\n".join(lines[start:end])))
            if len(found) >= limit:
                break
    return found


def list_files(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    directory = context.policy.resolve(_path(arguments))
    if not directory.is_dir():
        raise ValueError(f"directory does not exist: {directory}")
    pattern = arguments.get("pattern", "*")
    if not isinstance(pattern, str):
        raise ValueError("pattern must be a string")
    files = [item for item in sorted(directory.rglob("*")) if item.is_file() and fnmatch.fnmatch(item.name, pattern)][:context.policy.max_files]
    return ToolResult(True, {"path": str(directory), "files": [str(item.relative_to(context.policy.root)) for item in files], "count": len(files)})


def read_file(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    path = context.policy.readable_file(_path(arguments))
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError(f"unsupported text file type: {path.suffix or '(none)'}")
    lines = path.read_text(encoding="utf-8").splitlines()
    start = int(arguments.get("start_line", 1))
    end = int(arguments.get("end_line", len(lines)))
    if start < 1 or end < start:
        raise ValueError("line range must satisfy 1 <= start_line <= end_line")
    end = min(end, len(lines))
    passage = Evidence(path, start, end, "\n".join(lines[start - 1:end]))
    return ToolResult(True, {"path": str(path), "start_line": start, "end_line": end, "text": passage.text, "evidence_id": passage.id}, citations=(passage,))


def search_files(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    directory = context.policy.resolve(_path(arguments))
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    pattern = arguments.get("pattern", "*")
    if not isinstance(pattern, str):
        raise ValueError("pattern must be a string")
    candidates = [directory] if directory.is_file() else [item for item in directory.rglob("*") if item.is_file()]
    output: list[Evidence] = []
    for item in candidates[:context.policy.max_files]:
        if item.suffix.lower() not in TEXT_SUFFIXES or not fnmatch.fnmatch(item.name, pattern) or item.stat().st_size > context.policy.max_file_bytes:
            continue
        try:
            output.extend(_evidence(item, query, limit=8 - len(output)))
        except UnicodeDecodeError:
            continue
        if len(output) >= 8:
            break
    return ToolResult(True, [passage.as_dict() for passage in output], citations=tuple(output))


def _git(context: RunContext, *args: str) -> ToolResult:
    if not (context.policy.root / ".git").exists():
        return ToolResult(False, {}, "workspace is not a Git repository")
    completed = subprocess.run(["git", "-C", str(context.policy.root), *args], capture_output=True, text=True, timeout=10, check=False)
    if completed.returncode:
        return ToolResult(False, {}, completed.stderr.strip() or "git command failed")
    return ToolResult(True, {"output": completed.stdout[:100_000]})


def git_status(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    return _git(context, "status", "--short")


def git_diff(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    path = arguments.get("path")
    if path is not None:
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        context.policy.resolve(path)
        return _git(context, "diff", "--", path)
    return _git(context, "diff")
