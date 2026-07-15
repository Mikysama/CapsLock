"""Thin model-tool adapters over the application action coordinator."""

from __future__ import annotations

from typing import Any

from ..application import ActionCoordinator
from ..domain import ActionType, ChangeInfo, CommandInfo, ExternalActionInfo
from .core import RunContext, ToolResult


def coordinator(context: RunContext) -> ActionCoordinator:
    if context.actions is not None:
        return context.actions
    if context.store is None:
        raise ValueError("action storage is unavailable")
    return ActionCoordinator(store=context.store, policy=context.policy, session_id=context.session_id, run_id=context.run_id, event=context.event, permission_mode=context.permission_mode)


def change_data(change: ChangeInfo) -> dict[str, object]:
    return {"change_id": change.id, "path": change.path, "operation": change.operation, "summary": change.summary, "status": change.status, "result_kind": change.result_kind, "diff": change.diff}


def command_data(command: CommandInfo) -> dict[str, object]:
    return {"command_id": command.id, "template": command.template, "argv": list(command.argv), "cwd": command.cwd, "summary": command.summary, "status": command.status, "result_kind": command.result_kind, "exit_code": command.exit_code, "stdout": command.stdout, "stderr": command.stderr}


def external_data(action: ExternalActionInfo) -> dict[str, object]:
    return {"action_id": action.id, "kind": action.kind, "summary": action.summary, "status": action.status, "result_kind": action.result_kind, "result": action.result, "error": action.error}


def propose_web_search(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    query = arguments.get("query")
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    action = coordinator(context).propose(ActionType.WEB_SEARCH, query=query)
    assert isinstance(action, ExternalActionInfo)
    return ToolResult(True, external_data(action))


def propose_web_fetch(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    url = arguments.get("url")
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    action = coordinator(context).propose(ActionType.WEB_FETCH, url=url)
    assert isinstance(action, ExternalActionInfo)
    return ToolResult(True, external_data(action))


def propose_mcp_connect(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    server = arguments.get("server")
    if not isinstance(server, str):
        raise ValueError("server must be a string")
    action = coordinator(context).propose(ActionType.MCP_CONNECT, server=server)
    assert isinstance(action, ExternalActionInfo)
    return ToolResult(True, external_data(action))


def propose_mcp_call(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    server, tool, payload = arguments.get("server"), arguments.get("tool"), arguments.get("arguments")
    if not isinstance(server, str) or not isinstance(tool, str) or not isinstance(payload, dict):
        raise ValueError("server, tool, and arguments must be provided")
    action = coordinator(context).propose(ActionType.MCP_CALL, server=server, tool=tool, arguments=payload)
    assert isinstance(action, ExternalActionInfo)
    return ToolResult(True, external_data(action))


def propose_command(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    template, target, cwd = arguments.get("template"), arguments.get("target"), arguments.get("cwd", ".")
    if not isinstance(template, str) or target is not None and not isinstance(target, str) or not isinstance(cwd, str):
        raise ValueError("template, target, and cwd must be strings")
    command = coordinator(context).propose(ActionType.COMMAND, template=template, target=target, cwd=cwd)
    assert isinstance(command, CommandInfo)
    return ToolResult(True, command_data(command))


def run_command(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    command_id = arguments.get("command_id")
    if not isinstance(command_id, str):
        raise ValueError("command_id must be a string")
    command = coordinator(context).execute_approved(command_id)
    assert isinstance(command, CommandInfo)
    return ToolResult(True, command_data(command))


def discard_command(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    command_id = arguments.get("command_id")
    if not isinstance(command_id, str):
        raise ValueError("command_id must be a string")
    command = coordinator(context).reject(command_id)
    assert isinstance(command, CommandInfo)
    return ToolResult(True, command_data(command))


def propose_file_edit(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    path, old_text, new_text = arguments.get("path"), arguments.get("old_text"), arguments.get("new_text")
    summary = arguments.get("summary", "")
    if not all(isinstance(item, str) for item in (path, old_text, new_text, summary)):
        raise ValueError("path, old_text, new_text, and summary must be strings")
    change = coordinator(context).propose(ActionType.FILE_EDIT, path=path, old_text=old_text, new_text=new_text, summary=summary)
    assert isinstance(change, ChangeInfo)
    return ToolResult(True, change_data(change))


def propose_file_create(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    path, content, summary = arguments.get("path"), arguments.get("content"), arguments.get("summary", "")
    if not all(isinstance(item, str) for item in (path, content, summary)):
        raise ValueError("path, content, and summary must be strings")
    change = coordinator(context).propose(ActionType.FILE_CREATE, path=path, content=content, summary=summary)
    assert isinstance(change, ChangeInfo)
    return ToolResult(True, change_data(change))


def apply_change(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    change_id = arguments.get("change_id")
    if not isinstance(change_id, str):
        raise ValueError("change_id must be a string")
    change = coordinator(context).execute_approved(change_id)
    assert isinstance(change, ChangeInfo)
    return ToolResult(True, change_data(change))


def discard_change(context: RunContext, arguments: dict[str, Any]) -> ToolResult:
    change_id = arguments.get("change_id")
    if not isinstance(change_id, str):
        raise ValueError("change_id must be a string")
    change = coordinator(context).reject(change_id)
    assert isinstance(change, ChangeInfo)
    return ToolResult(True, change_data(change))
