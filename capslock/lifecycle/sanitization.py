"""Secret-safe portable and local backup sanitization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..security import redact


def sanitize_mcp(document: dict[str, Any]) -> dict[str, Any]:
    safe = json.loads(json.dumps(document))
    for server in safe.get("servers", {}).values():
        if isinstance(server, dict) and isinstance(server.get("env"), dict):
            server["env"] = {}
            server["enabled"] = False
    return safe


def sanitize_config(source: Path, target: Path) -> bool:
    try:
        import tomlkit

        document = tomlkit.parse(source.read_text(encoding="utf-8"))
    except Exception:
        target.write_text(
            "# Invalid source configuration was omitted to avoid copying credentials.\n",
            encoding="utf-8",
        )
        return True
    removed = False

    def clean(table: Any) -> None:
        nonlocal removed
        if not hasattr(table, "items"):
            return
        for key, value in list(table.items()):
            normalized = str(key).casefold().replace("-", "_")
            if normalized in {
                "api_key",
                "tavily_api_key",
                "authorization",
                "password",
                "secret",
            }:
                del table[key]
                removed = True
            else:
                clean(value)

    clean(document)
    target.write_text(tomlkit.dumps(document), encoding="utf-8")
    return removed


def redact_portable(value: Any) -> Any:
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            secret_key = any(
                marker in normalized
                for marker in ("api_key", "authorization", "secret", "password")
            ) or ("token" in normalized and not normalized.endswith("_tokens"))
            output[str(key)] = (
                "<redacted>"
                if secret_key and isinstance(item, str)
                else redact_portable(item)
            )
        return output
    if isinstance(value, list):
        return [redact_portable(item) for item in value]
    return redact(value) if isinstance(value, str) else value
