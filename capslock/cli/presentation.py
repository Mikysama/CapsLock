"""Safe view models shared by terminal frontends."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

from ..domain import ActionRecord, ActionType
from ..security import redact


@dataclass(frozen=True)
class ActionPresentation:
    title: str
    subtitle: str
    target: str | None
    preview: str | None
    preview_kind: str = "text"


@dataclass(frozen=True)
class ToolPresentation:
    identifier: str
    name: str
    category: str
    title: str
    detail: str | None = None
    target: str | None = None
    outcome: str | None = None
    ok: bool | None = None
    duration_ms: int | None = None

    @property
    def groupable(self) -> bool:
        return self.category in {"read", "search"}


_FALLBACK_CATEGORIES = {
    "list_files": "read",
    "read_file": "read",
    "read_skill_resource": "read",
    "get_memory": "read",
    "load_skill": "read",
    "search_files": "search",
    "search_memories": "search",
    "git_status": "search",
    "git_diff": "search",
    "list_external_sources": "search",
}


def present_tool(data: dict[str, Any], *, sequence: int = 0) -> ToolPresentation:
    """Read only the versioned, allowlisted event presentation fields."""

    name = str(data.get("name", "unknown"))
    raw = data.get("presentation")
    value = raw if isinstance(raw, dict) and raw.get("version") == 1 else {}
    ok = data.get("ok")
    duration = data.get("duration_ms")
    return ToolPresentation(
        identifier=str(data.get("tool_call_id", f"tool-{sequence}")),
        name=name,
        category=str(value.get("category", _FALLBACK_CATEGORIES.get(name, "other"))),
        title=str(value.get("title", name.replace("_", " ").capitalize())),
        detail=_optional_text(value.get("detail")),
        target=_optional_text(value.get("target")),
        outcome=_optional_text(value.get("outcome")),
        ok=bool(ok) if isinstance(ok, bool) else None,
        duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
    )


def present_action(action: ActionRecord) -> ActionPresentation:
    """Build a local-only, redacted approval preview from allowlisted fields."""

    request = redact(action.request)
    risk = action.risk_level or "unknown risk"
    subtitle = f"{action.type.value.replace('_', ' ')} · {risk}"
    target: str | None = None
    preview: str | None = None
    preview_kind = "text"

    if action.type in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
        target = _optional_text(request.get("path"))
        preview = _optional_text(request.get("diff"))
        preview_kind = "diff"
    elif action.type is ActionType.COMMAND:
        target = _optional_text(request.get("cwd")) or "."
        argv = request.get("argv")
        if isinstance(argv, list):
            preview = shlex.join(str(item) for item in argv)
        else:
            preview = _optional_text(request.get("template"))
        preview_kind = "command"
    elif action.type in {ActionType.WEB_SEARCH, ActionType.WEB_FETCH}:
        target = _optional_text(request.get("url"))
        preview = _optional_text(request.get("query")) or target
    elif action.type in {ActionType.MCP_CONNECT, ActionType.MCP_CALL}:
        target = _optional_text(request.get("server"))
        tool = _optional_text(request.get("tool"))
        preview = f"tool: {tool}" if tool else None

    if preview is None:
        allowed = {
            key: request[key]
            for key in ("path", "template", "cwd", "query", "url", "server", "tool")
            if key in request
        }
        preview = json.dumps(allowed, ensure_ascii=False, indent=2) if allowed else None

    return ActionPresentation(
        action.summary,
        subtitle,
        target,
        truncate_preview(preview) if preview else None,
        preview_kind,
    )


def truncate_preview(value: str, *, max_lines: int = 40, max_bytes: int = 4096) -> str:
    lines = value.splitlines()
    truncated = len(lines) > max_lines
    text = "\n".join(lines[:max_lines])
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", "ignore")
        truncated = True
    return text.rstrip() + ("\n… preview truncated" if truncated else "")


def _optional_text(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None
