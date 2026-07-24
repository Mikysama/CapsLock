"""Bounded PDF and Jupyter Notebook tools."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ...domain import ActionType
from .actions import execute_action_tool
from ..contracts import ExecutionContext, ToolContent, ToolExecution, ToolOutcome


def _document_limit(context: ExecutionContext, name: str, default: int) -> int:
    settings = context.runtime_state.get("document_settings")
    return int(getattr(settings, name, default))


async def _program(*arguments: str, timeout: float = 30) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError("document backend timed out") from None
    return process.returncode or 0, stdout, stderr


def _pages(value: object, total: int, maximum: int) -> list[int]:
    if value is None:
        if total > maximum:
            raise ValueError("large PDF requires an explicit pages selection")
        return list(range(1, total + 1))
    result: set[int] = set()
    values = value if isinstance(value, list) else str(value).split(",")
    for raw in values:
        if isinstance(raw, int) and not isinstance(raw, bool):
            result.add(raw)
            continue
        part = str(raw).strip()
        if "-" in part:
            start, end = part.split("-", 1)
            result.update(range(int(start), int(end) + 1))
        else:
            result.add(int(part))
    selected = sorted(result)
    if not selected or len(selected) > maximum:
        raise ValueError(f"select between 1 and {maximum} PDF pages")
    if selected[0] < 1 or selected[-1] > total:
        raise ValueError("PDF page selection is outside the document")
    return selected


async def read_pdf(context: ExecutionContext, arguments: dict[str, Any]) -> ToolOutcome:
    maximum_bytes = _document_limit(context, "max_pdf_bytes", 50 * 1024 * 1024)
    path = context.policy.readable_binary_file(
        str(arguments["path"]), max_bytes=maximum_bytes
    )
    if path.suffix.casefold() != ".pdf":
        return ToolOutcome.failure("path is not a PDF", code="unsupported_document")
    programs = {
        name: shutil.which(name) for name in ("pdfinfo", "pdftotext", "pdftoppm")
    }
    if not programs["pdfinfo"] or not programs["pdftotext"]:
        return ToolOutcome.failure(
            "Poppler is unavailable; install pdfinfo, pdftotext, and pdftoppm",
            code="pdf_backend_unavailable",
        )
    code, stdout, stderr = await _program(str(programs["pdfinfo"]), str(path))
    if code:
        return ToolOutcome.failure(
            stderr.decode("utf-8", "replace").strip() or "pdfinfo failed",
            code="invalid_pdf",
        )
    total = 0
    for line in stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("Pages:"):
            total = int(line.split(":", 1)[1].strip())
            break
    if total < 1:
        return ToolOutcome.failure("PDF page count is unavailable", code="invalid_pdf")
    selected = _pages(
        arguments.get("pages"),
        total,
        _document_limit(context, "max_pdf_pages", 10),
    )
    render = bool(arguments.get("render_pages", False))
    text_pages: list[dict[str, object]] = []
    content: list[ToolContent] = []
    for page in selected:
        code, page_text, _ = await _program(
            str(programs["pdftotext"]),
            "-f",
            str(page),
            "-l",
            str(page),
            "-layout",
            str(path),
            "-",
        )
        text = page_text.decode("utf-8", "replace").strip() if not code else ""
        text_pages.append({"page": page, "text": text})
        if render or not text:
            if not programs["pdftoppm"]:
                continue
            with tempfile.TemporaryDirectory(prefix="capslock-pdf-") as directory:
                prefix = str(Path(directory) / "page")
                code, _, _ = await _program(
                    str(programs["pdftoppm"]),
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-jpeg",
                    "-r",
                    "144",
                    "-singlefile",
                    str(path),
                    prefix,
                    timeout=60,
                )
                image = Path(prefix + ".jpg")
                if not code and image.is_file():
                    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
                    content.append(
                        ToolContent.image(
                            f"data:image/jpeg;base64,{encoded}", "image/jpeg"
                        )
                    )
    relative = str(path.relative_to(context.policy.root))
    return ToolOutcome.success(
        {
            "path": relative,
            "page_count": total,
            "pages": text_pages,
            "rendered_pages": len(content),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
        content=tuple(content),
        audit_data={"path": relative, "pages": selected},
    )


def _read_notebook_document(
    context: ExecutionContext, path_text: str
) -> tuple[Path, dict[str, Any], bytes]:
    path = context.policy.readable_file(path_text)
    if path.suffix.casefold() != ".ipynb":
        raise ValueError("path is not a Jupyter Notebook")
    raw = path.read_bytes()
    maximum = _document_limit(context, "max_notebook_bytes", 10 * 1024 * 1024)
    if len(raw) > maximum:
        raise ValueError("Notebook exceeds the configured size limit")
    document = json.loads(raw)
    if not isinstance(document, dict) or not isinstance(document.get("cells"), list):
        raise ValueError("invalid Jupyter Notebook JSON")
    return path, document, raw


async def read_notebook(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    path, document, raw = await asyncio.to_thread(
        _read_notebook_document, context, str(arguments["path"])
    )
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", 20))
    maximum = _document_limit(context, "max_notebook_cells", 50)
    if offset < 0 or not 1 <= limit <= maximum:
        raise ValueError(f"Notebook range must use limit 1..{maximum}")
    include_outputs = bool(arguments.get("include_outputs", False))
    output_limit = _document_limit(context, "max_cell_output_bytes", 65_536)
    values = []
    for index, cell in enumerate(document["cells"][offset : offset + limit], offset):
        if not isinstance(cell, dict):
            continue
        value: dict[str, object] = {
            "index": index,
            "id": cell.get("id"),
            "cell_type": cell.get("cell_type"),
            "source": "".join(cell.get("source", [])),
            "metadata": cell.get("metadata", {}),
        }
        if include_outputs and isinstance(cell.get("outputs"), list):
            sanitized_outputs = _sanitize_outputs(cell["outputs"])
            encoded = json.dumps(sanitized_outputs, ensure_ascii=False, default=str)
            value["outputs"] = encoded[:output_limit]
            value["outputs_truncated"] = len(encoded) > output_limit
        values.append(value)
    relative = str(path.relative_to(context.policy.root))
    return ToolOutcome.success(
        {
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "total_cells": len(document["cells"]),
            "offset": offset,
            "cells": values,
            "truncated": offset + len(values) < len(document["cells"]),
        }
    )


def _sanitize_outputs(outputs: list[object]) -> list[dict[str, object]]:
    sanitized: list[dict[str, object]] = []
    for raw in outputs:
        if not isinstance(raw, dict):
            continue
        item = {
            key: raw[key]
            for key in ("output_type", "name", "text", "ename", "evalue", "traceback")
            if key in raw
        }
        data = raw.get("data")
        if isinstance(data, dict):
            item["data"] = {
                key: value
                for key, value in data.items()
                if key in {"text/plain", "text/markdown", "application/json"}
            }
        sanitized.append(item)
    return sanitized


async def edit_notebook(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    path, document, raw = await asyncio.to_thread(
        _read_notebook_document, context, str(arguments["path"])
    )
    expected = str(arguments["expected_sha256"])
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Notebook hash does not match expected_sha256")
    cells = document["cells"]
    selector_id = arguments.get("cell_id")
    selector_index = arguments.get("index")
    index: int
    if selector_id is not None:
        matches = [
            i
            for i, cell in enumerate(cells)
            if isinstance(cell, dict) and cell.get("id") == selector_id
        ]
        if len(matches) != 1:
            raise ValueError("cell_id must identify exactly one cell")
        index = matches[0]
    elif isinstance(selector_index, int) and not isinstance(selector_index, bool):
        index = selector_index
    else:
        raise ValueError("provide cell_id or index")
    mode = str(arguments["mode"])
    if mode == "delete":
        if not 0 <= index < len(cells):
            raise ValueError("Notebook cell index is out of range")
        cells.pop(index)
    elif mode == "replace":
        if not 0 <= index < len(cells):
            raise ValueError("Notebook cell index is out of range")
        source = arguments.get("source")
        if not isinstance(source, str):
            raise ValueError("replace requires source")
        cells[index]["source"] = source.splitlines(keepends=True)
    elif mode == "insert":
        if not 0 <= index <= len(cells):
            raise ValueError("Notebook insertion index is out of range")
        source, cell_type = arguments.get("source"), arguments.get("cell_type")
        if not isinstance(source, str) or cell_type not in {"code", "markdown", "raw"}:
            raise ValueError("insert requires source and a valid cell_type")
        cell: dict[str, object] = {
            "cell_type": cell_type,
            "metadata": {},
            "source": source.splitlines(keepends=True),
        }
        if cell_type == "code":
            cell.update({"execution_count": None, "outputs": []})
        cells.insert(index, cell)
    else:
        raise ValueError("mode must be replace, insert, or delete")
    after = json.dumps(document, ensure_ascii=False, indent=1) + "\n"
    return await execute_action_tool(
        context,
        ActionType.NOTEBOOK_EDIT,
        {
            "path": str(path.relative_to(context.policy.root)),
            "replace_content": after,
            "expected_sha256": expected,
            "summary": f"{mode} notebook cell {index}",
            "cell_summary": {"mode": mode, "index": index},
        },
    )


def document_tools():
    from ..contracts import ResolvedToolPolicy, define_tool
    from .schemas import _schema, _str

    safe_read = ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "read_pdf",
            "Extract bounded PDF text and optionally render selected pages with Poppler.",
            _schema(
                {
                    "path": _str(),
                    "pages": {
                        "oneOf": [
                            _str(),
                            {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 1},
                            },
                        ]
                    },
                    "render_pages": {"type": "boolean"},
                },
                ["path"],
            ),
            read_pdf,
            policy=safe_read,
            deferred=True,
            search_hint="PDF document text pages images",
        ),
        define_tool(
            "read_notebook",
            "Read bounded Jupyter Notebook cells with optional sanitized outputs.",
            _schema(
                {
                    "path": _str(),
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "include_outputs": {"type": "boolean"},
                },
                ["path"],
            ),
            read_notebook,
            policy=safe_read,
            deferred=True,
            search_hint="Jupyter ipynb notebook cells outputs",
        ),
        define_tool(
            "edit_notebook",
            "Replace, insert, or delete a Notebook cell through a hash-revalidated Action.",
            _schema(
                {
                    "path": _str(),
                    "mode": {"type": "string", "enum": ["replace", "insert", "delete"]},
                    "cell_id": _str(),
                    "index": {"type": "integer", "minimum": 0},
                    "source": _str(),
                    "cell_type": {
                        "type": "string",
                        "enum": ["code", "markdown", "raw"],
                    },
                    "expected_sha256": _str(),
                },
                ["path", "mode", "expected_sha256"],
            ),
            edit_notebook,
            deferred=True,
            search_hint="edit Jupyter ipynb notebook cell",
        ),
    ]


__all__ = ["read_pdf", "read_notebook", "edit_notebook", "document_tools"]
