"""Declarative async tool catalog."""

from __future__ import annotations

from .async_adapters import (
    get_memory,
    git_diff,
    git_status,
    list_external_sources,
    list_files,
    load_skill,
    propose_command,
    propose_file_create,
    propose_file_edit,
    propose_mcp_call,
    propose_mcp_connect,
    propose_web_fetch,
    propose_web_search,
    read_file,
    read_skill_resource,
    search_files,
    search_memories,
    task_list_update,
    task_status_update,
)
from .async_core import Tool, ToolRegistry


def workspace_tools() -> ToolRegistry:
    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    return ToolRegistry(
        [
            Tool(
                "list_files",
                "List readable workspace files.",
                _schema({"path": _str(), "pattern": _str()}, ["path"]),
                list_files,
            ),
            Tool(
                "read_file",
                "Read a UTF-8 workspace file with evidence.",
                _schema(
                    {"path": _str(), "start_line": _int(), "end_line": _int()}, ["path"]
                ),
                read_file,
            ),
            Tool(
                "search_files",
                "Search readable workspace text and return evidence.",
                _schema(
                    {"path": _str(), "query": _str(), "pattern": _str()},
                    ["path", "query"],
                ),
                search_files,
            ),
            Tool("git_status", "Show the Git working-tree status.", empty, git_status),
            Tool(
                "git_diff",
                "Show the Git diff, optionally for one path.",
                _schema({"path": _str()}),
                git_diff,
            ),
            Tool(
                "task_list_update",
                "Replace the run task list.",
                _schema({"items": {"type": "array", "items": _str()}}, ["items"]),
                task_list_update,
            ),
            Tool(
                "task_status_update",
                "Update one task status.",
                _schema(
                    {
                        "task_id": _str(),
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending",
                                "running",
                                "blocked",
                                "completed",
                                "failed",
                                "cancelled",
                            ],
                        },
                    },
                    ["task_id", "status"],
                ),
                task_status_update,
            ),
            Tool(
                "list_external_sources",
                "List persisted untrusted Web sources.",
                empty,
                list_external_sources,
            ),
            Tool(
                "search_memories",
                "Search user-managed memories.",
                _schema(
                    {
                        "query": _str(),
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    ["query"],
                ),
                search_memories,
            ),
            Tool(
                "get_memory",
                "Read one visible memory.",
                _schema({"memory_id": _str()}, ["memory_id"]),
                get_memory,
            ),
            Tool(
                "load_skill",
                "Load matching local Skill instructions.",
                _schema({"name": _str()}, ["name"]),
                load_skill,
            ),
            Tool(
                "read_skill_resource",
                "Read a loaded Skill text resource.",
                _schema(
                    {
                        "name": _str(),
                        "path": _str(),
                        "start_line": _int(),
                        "end_line": _int(),
                    },
                    ["name", "path"],
                ),
                read_skill_resource,
            ),
            Tool(
                "propose_file_edit",
                "Propose an exact text replacement without writing.",
                _schema(
                    {
                        "path": _str(),
                        "old_text": _str(),
                        "new_text": _str(),
                        "summary": _str(),
                    },
                    ["path", "old_text", "new_text"],
                ),
                propose_file_edit,
            ),
            Tool(
                "propose_file_create",
                "Propose creating a text file without writing.",
                _schema(
                    {"path": _str(), "content": _str(), "summary": _str()},
                    ["path", "content"],
                ),
                propose_file_create,
            ),
            Tool(
                "propose_command",
                "Propose an approved fixed command template.",
                _schema(
                    {
                        "template": {
                            "type": "string",
                            "enum": [
                                "pytest",
                                "npm_test",
                                "npm_build",
                                "ruff_check",
                                "prettier_check",
                            ],
                        },
                        "target": _str(),
                        "cwd": _str(),
                    },
                    ["template"],
                ),
                propose_command,
            ),
            Tool(
                "propose_web_search",
                "Propose a Tavily Web search.",
                _schema({"query": _str()}, ["query"]),
                propose_web_search,
            ),
            Tool(
                "propose_web_fetch",
                "Propose fetching a public HTTP URL.",
                _schema({"url": _str()}, ["url"]),
                propose_web_fetch,
            ),
            Tool(
                "propose_mcp_connect",
                "Propose connecting to an allowed stdio MCP server.",
                _schema({"server": _str()}, ["server"]),
                propose_mcp_connect,
            ),
            Tool(
                "propose_mcp_call",
                "Propose calling an allowed MCP tool.",
                _schema(
                    {"server": _str(), "tool": _str(), "arguments": {"type": "object"}},
                    ["server", "tool", "arguments"],
                ),
                propose_mcp_call,
            ),
        ]
    )


def _str() -> dict[str, object]:
    return {"type": "string"}


def _int() -> dict[str, object]:
    return {"type": "integer"}


def _schema(
    properties: dict[str, object], required: list[str] | None = None
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema
