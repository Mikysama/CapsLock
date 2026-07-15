"""Declarative catalog of tools exposed to the model."""

from __future__ import annotations

from .actions import apply_change, discard_change, discard_command, propose_command, propose_file_create, propose_file_edit, propose_mcp_call, propose_mcp_connect, propose_web_fetch, propose_web_search, run_command
from .core import Tool, ToolRegistry
from .tasks import list_external_sources, task_list_update, task_status_update
from .workspace import git_diff, git_status, list_files, read_file, search_files


def workspace_tools() -> ToolRegistry:
    string_path = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}
    return ToolRegistry([
        Tool("list_files", "List files under a workspace directory. Read-only.", {**string_path, "properties": {"path": {"type": "string"}, "pattern": {"type": "string"}}}, list_files),
        Tool("read_file", "Read a UTF-8 text or source file with line-addressable evidence.", {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"], "additionalProperties": False}, read_file),
        Tool("search_files", "Search text and source files, returning evidence with paths and lines.", {"type": "object", "properties": {"path": {"type": "string"}, "query": {"type": "string"}, "pattern": {"type": "string"}}, "required": ["path", "query"], "additionalProperties": False}, search_files),
        Tool("git_status", "Show Git working-tree status. Read-only.", {"type": "object", "properties": {}, "additionalProperties": False}, git_status),
        Tool("git_diff", "Show Git diff, optionally limited to a workspace path. Read-only.", {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False}, git_diff),
        Tool("task_list_update", "Update the session task list without writing user files.", {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}}, "required": ["items"], "additionalProperties": False}, task_list_update),
        Tool("task_status_update", "Update one session task status after work completes, fails, or is blocked.", {"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "running", "blocked", "completed", "failed", "cancelled"]}}, "required": ["task_id", "status"], "additionalProperties": False}, task_status_update),
        Tool("list_external_sources", "List the session's untrusted external Web sources for citation and review.", {"type": "object", "properties": {}, "additionalProperties": False}, list_external_sources),
        Tool("propose_file_edit", "Propose one exact text replacement. This never writes a file; the user must approve before application.", {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "summary": {"type": "string"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}, propose_file_edit),
        Tool("propose_file_create", "Propose creation of one text file. This never writes a file; the user must approve before application.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "summary": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}, propose_file_create),
        Tool("apply_change", "Apply an already user-approved change proposal. Never call this before approval.", {"type": "object", "properties": {"change_id": {"type": "string"}}, "required": ["change_id"], "additionalProperties": False}, apply_change),
        Tool("discard_change", "Discard a pending change proposal without writing files.", {"type": "object", "properties": {"change_id": {"type": "string"}}, "required": ["change_id"], "additionalProperties": False}, discard_change),
        Tool("propose_command", "Propose a fixed, approved command template. This never starts a process; the user must approve it in the CLI.", {"type": "object", "properties": {"template": {"type": "string", "enum": ["pytest", "npm_test", "npm_build", "ruff_check", "prettier_check"]}, "target": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["template"], "additionalProperties": False}, propose_command),
        Tool("run_command", "Run an already user-approved command proposal. Never call this before approval.", {"type": "object", "properties": {"command_id": {"type": "string"}}, "required": ["command_id"], "additionalProperties": False}, run_command),
        Tool("discard_command", "Discard a pending command proposal without starting a process.", {"type": "object", "properties": {"command_id": {"type": "string"}}, "required": ["command_id"], "additionalProperties": False}, discard_command),
        Tool("propose_web_search", "Propose a Tavily web search. This sends no network request until the user approves it.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}, propose_web_search),
        Tool("propose_web_fetch", "Propose fetching an external http/https URL. This sends no network request until user approval.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}, propose_web_fetch),
        Tool("propose_mcp_connect", "Propose starting an allowed local stdio MCP server and listing its tools.", {"type": "object", "properties": {"server": {"type": "string"}}, "required": ["server"], "additionalProperties": False}, propose_mcp_connect),
        Tool("propose_mcp_call", "Propose calling an allowed MCP tool. Never call MCP tools without approval.", {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["server", "tool", "arguments"], "additionalProperties": False}, propose_mcp_call),
    ])
