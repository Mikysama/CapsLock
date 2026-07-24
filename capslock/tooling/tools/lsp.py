"""Direct-capability semantic code tools backed by the managed LSP runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ...ports import LspClientPort
from ..contracts import (
    ExecutionContext,
    ResolvedToolPolicy,
    ToolDefinition,
    ToolOutcome,
    define_tool,
)


def lsp_tools(runtime: LspClientPort) -> list[ToolDefinition]:
    if not runtime.available:
        return []

    async def execute(
        context: ExecutionContext, arguments: dict[str, Any], *, operation: str
    ) -> ToolOutcome:
        path = context.policy.readable_file(str(arguments["path"]))
        position = {
            "line": int(arguments.get("line", 1)) - 1,
            "character": int(arguments.get("column", 1)) - 1,
        }
        document = {"uri": path.as_uri()}
        if operation == "definition":
            raw = await runtime.request(
                str(path),
                "textDocument/definition",
                {"textDocument": document, "position": position},
            )
        elif operation == "references":
            raw = await runtime.request(
                str(path),
                "textDocument/references",
                {
                    "textDocument": document,
                    "position": position,
                    "context": {"includeDeclaration": True},
                },
            )
        elif operation == "hover":
            raw = await runtime.request(
                str(path),
                "textDocument/hover",
                {"textDocument": document, "position": position},
            )
            return ToolOutcome.success({"path": _relative(context, path), "hover": raw})
        elif operation == "symbols":
            raw = await runtime.request(
                str(path), "textDocument/documentSymbol", {"textDocument": document}
            )
            return ToolOutcome.success(
                {
                    "path": _relative(context, path),
                    "symbols": _symbols(raw)[:200],
                    "truncated": len(_symbols(raw)) > 200,
                }
            )
        elif operation == "search_symbols":
            raw = await runtime.request(
                str(path), "workspace/symbol", {"query": str(arguments["query"])}
            )
        elif operation == "implementations":
            raw = await runtime.request(
                str(path),
                "textDocument/implementation",
                {"textDocument": document, "position": position},
            )
        else:
            prepared = await runtime.request(
                str(path),
                "textDocument/prepareCallHierarchy",
                {"textDocument": document, "position": position},
            )
            items = (
                prepared
                if isinstance(prepared, list)
                else ([prepared] if isinstance(prepared, dict) else [])
            )
            values: list[object] = []
            method = (
                "callHierarchy/incomingCalls"
                if operation == "callers"
                else "callHierarchy/outgoingCalls"
            )
            for item in items[:10]:
                response = await runtime.request(str(path), method, {"item": item})
                if isinstance(response, list):
                    values.extend(response)
            raw = values
        all_locations = _locations(raw, context)
        limit = int(arguments.get("limit", 200))
        locations = all_locations[:limit]
        return ToolOutcome.success(
            {
                "locations": locations,
                "count": len(locations),
                "total_count": len(all_locations),
                "truncated": len(all_locations) > limit,
            }
        )

    position_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer", "minimum": 1},
            "column": {"type": "integer", "minimum": 1},
        },
        "required": ["path", "line", "column"],
        "additionalProperties": False,
    }
    tools: list[ToolDefinition] = []
    descriptions = {
        "find_definition": ("definition", "Find the definition at a source position."),
        "find_references": (
            "references",
            "Find references to the symbol at a source position.",
        ),
        "inspect_symbol": ("hover", "Inspect type and documentation for a symbol."),
        "find_implementations": (
            "implementations",
            "Find implementations of a symbol.",
        ),
        "find_callers": ("callers", "Find callers of a callable symbol."),
        "find_callees": ("callees", "Find callees of a callable symbol."),
    }
    for name, (operation, description) in descriptions.items():

        async def bound(context, arguments, reporter, *, selected=operation):
            del reporter
            return await execute(context, arguments, operation=selected)

        tools.append(
            define_tool(
                name,
                description,
                position_schema,
                bound,
                policy=ResolvedToolPolicy.safe_read(),
                deferred=True,
                search_hint="LSP code semantic symbol navigation",
            )
        )

    async def symbols(context, arguments, reporter):
        del reporter
        return await execute(context, arguments, operation="symbols")

    tools.append(
        define_tool(
            "list_symbols",
            "List symbols declared in a source file.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            symbols,
            policy=ResolvedToolPolicy.safe_read(),
            deferred=True,
            search_hint="LSP code symbols outline",
        )
    )

    async def search(context, arguments, reporter):
        del reporter
        return await execute(context, arguments, operation="search_symbols")

    tools.append(
        define_tool(
            "search_symbols",
            "Search workspace symbols using the language server.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["path", "query"],
                "additionalProperties": False,
            },
            search,
            policy=ResolvedToolPolicy.safe_read(),
            deferred=True,
            search_hint="LSP workspace code symbol search",
        )
    )
    return tools


def _relative(context: ExecutionContext, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(context.policy.root))
    except ValueError:
        return str(path)


def _locations(raw: object, context: ExecutionContext) -> list[dict[str, object]]:
    values = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        location = item.get("location", item)
        if "from" in item:
            location = item["from"]
        if "to" in item:
            location = item["to"]
        if not isinstance(location, dict):
            continue
        uri = location.get("uri") or location.get("targetUri")
        range_value = location.get("range") or location.get("targetSelectionRange")
        if not isinstance(uri, str) or not isinstance(range_value, dict):
            continue
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            continue
        path = Path(unquote(parsed.path))
        start, end = range_value.get("start", {}), range_value.get("end", {})
        value = {
            "path": _relative(context, path),
            "range": {
                "start": {
                    "line": int(start.get("line", 0)) + 1,
                    "column": int(start.get("character", 0)) + 1,
                },
                "end": {
                    "line": int(end.get("line", 0)) + 1,
                    "column": int(end.get("character", 0)) + 1,
                },
            },
            "symbol_kind": item.get("kind"),
            "preview": _preview(path, int(start.get("line", 0))),
        }
        evidence = json.dumps(value, sort_keys=True, separators=(",", ":"))
        value["evidence_id"] = (
            "lsp_" + hashlib.sha256(evidence.encode()).hexdigest()[:24]
        )
        result.append(value)
    return result


def _preview(path: Path, line: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[line].strip()[:300] if 0 <= line < len(lines) else ""
    except (OSError, UnicodeError):
        return ""


def _symbols(raw: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []

    def visit(items: object) -> None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    key: item.get(key)
                    for key in (
                        "name",
                        "kind",
                        "detail",
                        "range",
                        "selectionRange",
                        "location",
                    )
                    if key in item
                }
            )
            visit(item.get("children"))

    visit(raw)
    return result
