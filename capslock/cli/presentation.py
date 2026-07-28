"""Safe view models shared by terminal frontends."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..domain import ActionRecord, ActionType
from ..security import redact


@dataclass(frozen=True)
class ActionPresentation:
    title: str
    subtitle: str
    target: str | None
    preview: str | None
    preview_kind: str = "text"
    category: str = "generic"
    risk_reason: str | None = None
    rollback: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    permission_rules: tuple[tuple[str, str], ...] = ()


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
    category = "generic"
    metadata: list[tuple[str, str]] = []

    if action.type in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
        category = "file"
        target = _optional_text(request.get("path"))
        preview = _optional_text(request.get("diff"))
        preview_kind = "diff"
        if target:
            metadata.extend((("File", target.rsplit("/", 1)[-1]), ("Path", target)))
        if preview:
            added = sum(
                1
                for line in preview.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            removed = sum(
                1
                for line in preview.splitlines()
                if line.startswith("-") and not line.startswith("---")
            )
            metadata.append(("Changes", f"+{added} / -{removed}"))
    elif action.type is ActionType.COMMAND:
        category = "shell"
        target = _optional_text(request.get("cwd")) or "."
        argv = request.get("argv")
        if isinstance(argv, list):
            preview = shlex.join(str(item) for item in argv)
        else:
            preview = _optional_text(request.get("command")) or _optional_text(
                request.get("template")
            )
        preview_kind = "command"
        metadata.append(("Cwd", target))
        timeout = request.get("timeout_seconds", request.get("timeout"))
        if isinstance(timeout, (int, float)):
            metadata.append(("Timeout", f"{timeout:g}s"))
        safety = request.get("safety")
        if isinstance(safety, dict) and safety.get("reason"):
            metadata.append(("Command risk", str(safety["reason"])))
    elif action.type in {ActionType.WEB_SEARCH, ActionType.WEB_FETCH}:
        category = "web"
        target = _optional_text(request.get("url"))
        preview = _optional_text(request.get("query")) or target
        if target:
            metadata.extend(
                (("URL", target), ("Host", urlsplit(target).hostname or "—"))
            )
        elif preview:
            metadata.append(("Query", preview))
    elif action.type in {ActionType.MCP_CONNECT, ActionType.MCP_CALL}:
        category = "mcp"
        target = _optional_text(request.get("server"))
        tool = _optional_text(request.get("tool"))
        scope = {
            key: request[key]
            for key in ("arguments", "permissions", "capabilities")
            if key in request
        }
        preview = json.dumps(scope, ensure_ascii=False, indent=2) if scope else None
        if target:
            metadata.append(("Server", target))
        if request.get("plugin"):
            metadata.append(("Server", str(request["plugin"])))
        if tool:
            metadata.append(("Tool", tool))

    if preview is None:
        allowed = {
            key: request[key]
            for key in (
                "operation",
                "target",
                "path",
                "template",
                "cwd",
                "query",
                "url",
                "server",
                "plugin",
                "tool",
                "name",
                "branch",
            )
            if key in request
        }
        preview = json.dumps(allowed, ensure_ascii=False, indent=2) if allowed else None

    return ActionPresentation(
        action.summary,
        subtitle,
        target,
        truncate_preview(preview) if preview else None,
        preview_kind,
        category,
        action.risk_reason,
        action.rollback,
        tuple(metadata),
        _permission_rules(request),
    )


def present_permission_request(request: dict[str, object]) -> ActionPresentation:
    """Present non-Action tool approval through the same safe view model."""

    safe = redact(request)
    tool = str(safe.get("tool", "tool"))
    preview = _optional_text(safe.get("preview"))
    metadata = []
    if safe.get("server"):
        metadata.append(("Server", str(safe["server"])))
    if safe.get("url"):
        metadata.append(("URL", str(safe["url"])))
    return ActionPresentation(
        tool,
        "tool invocation · approval required",
        None,
        truncate_preview(preview) if preview else None,
        "text",
        "mcp" if tool.startswith("mcp") or safe.get("server") else "generic",
        _optional_text(safe.get("reason")),
        None,
        tuple(metadata),
        _permission_rules(safe),
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


def _permission_rules(request: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    permission = request.get("_permission")
    suggestions = (
        permission.get("suggestions")
        if isinstance(permission, dict)
        else request.get("suggestions", [])
    )
    result: list[tuple[str, str]] = []
    for item in suggestions if isinstance(suggestions, list) else ():
        if not isinstance(item, dict):
            continue
        destination = str(item.get("destination", ""))
        rule = {
            "behavior": item.get("behavior", "allow"),
            "tool": item.get("tool", "tool"),
            "constraints": item.get("constraints", {}),
        }
        result.append(
            (destination, json.dumps(rule, ensure_ascii=False, sort_keys=True))
        )
    return tuple(result)
