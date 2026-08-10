"""Filesystem tool registration."""

from .read import (
    list_files,
    read_file,
    read_image,
    read_tool_artifact,
    search_session_history,
)
from .search import glob_files, search_files, search_tools
from .write import create_file, edit_file, write_file


def filesystem_tools():
    from ...contracts import ResolvedToolPolicy, define_tool
    from ..schemas import _int, _schema, _str

    safe_read = ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "search_tools",
            "Search deferred plugin, MCP, and large-schema tools. Matches become available on the next model turn.",
            _schema(
                {
                    "query": _str(),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["query"],
            ),
            search_tools,
            policy=safe_read,
        ),
        define_tool(
            "list_files",
            "List readable workspace files.",
            _schema({"path": _str(), "pattern": _str()}, ["path"]),
            list_files,
            policy=safe_read,
        ),
        define_tool(
            "glob_files",
            "Find workspace files by path glob with stable ordering and truncation metadata.",
            _schema(
                {
                    "pattern": _str(),
                    "path": _str(),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
                    "include_hidden": {"type": "boolean"},
                },
                ["pattern"],
            ),
            glob_files,
            policy=safe_read,
        ),
        define_tool(
            "read_file",
            "Read a UTF-8 workspace file with evidence.",
            _schema(
                {"path": _str(), "start_line": _int(), "end_line": _int()}, ["path"]
            ),
            read_file,
            policy=safe_read,
        ),
        define_tool(
            "read_image",
            "Read a PNG, JPEG, GIF, or WebP workspace image as a rich image result.",
            _schema({"path": _str()}, ["path"]),
            read_image,
            policy=safe_read,
        ),
        define_tool(
            "search_session_history",
            "Search original messages, historical tool results, and artifact text in this session.",
            _schema(
                {
                    "query": _str(),
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["message", "tool_result", "artifact"]},
                        "uniqueItems": True,
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["query"],
            ),
            search_session_history,
            policy=safe_read,
        ),
        define_tool(
            "read_tool_artifact",
            "Read a session-scoped tool artifact in bounded chunks.",
            _schema(
                {
                    "artifact_id": _str(),
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 16384},
                },
                ["artifact_id"],
            ),
            read_tool_artifact,
            policy=safe_read,
            inline_result_bytes=16_384,
        ),
        define_tool(
            "search_files",
            "Search readable workspace text and return evidence.",
            _schema(
                {
                    "path": _str(),
                    "query": _str(),
                    "glob": _str(),
                    "context": {"type": "integer", "minimum": 0, "maximum": 20},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["path", "query"],
            ),
            search_files,
            policy=safe_read,
        ),
        define_tool(
            "edit_file",
            "Apply an exact text replacement through the durable approval workflow.",
            _schema(
                {
                    "path": _str(),
                    "old_text": _str(),
                    "new_text": _str(),
                    "summary": _str(),
                },
                ["path", "old_text", "new_text"],
            ),
            edit_file,
        ),
        define_tool(
            "create_file",
            "Create a text file through the durable approval workflow.",
            _schema(
                {"path": _str(), "content": _str(), "summary": _str()},
                ["path", "content"],
            ),
            create_file,
        ),
        define_tool(
            "write_file",
            "Write complete text content with a required read hash precondition through the durable Action workflow.",
            _schema(
                {
                    "path": _str(),
                    "content": _str(),
                    "expected_sha256": {"type": ["string", "null"]},
                    "summary": _str(),
                },
                ["path", "content", "expected_sha256"],
            ),
            write_file,
        ),
    ]


__all__ = ["filesystem_tools"]
