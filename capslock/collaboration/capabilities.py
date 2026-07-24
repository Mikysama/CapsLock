"""Child capability attenuation and action contract checks."""

from __future__ import annotations

from urllib.parse import urlparse

from ..domain import ActionRecord, ActionType
from .models import AgentTaskContract, CapabilityKind


class ChildCapabilityPolicy:
    def __init__(self, contract: AgentTaskContract) -> None:
        self.contract = contract
        self.grants = tuple(contract.capabilities)

    def tool_allowlist(self) -> set[str]:
        allowed = {
            "list_files",
            "glob_files",
            "read_file",
            "search_files",
            "git_status",
            "git_diff",
            "create_task",
            "list_tasks",
            "get_task",
            "update_task",
        }
        kinds = {item.kind for item in self.grants}
        if CapabilityKind.WORKSPACE_WRITE in kinds:
            allowed.update({"edit_file", "create_file", "write_file"})
        if CapabilityKind.COMMAND in kinds:
            allowed.update({"shell", "process_output", "process_stop"})
        if CapabilityKind.WEB in kinds:
            allowed.update({"web_search", "web_fetch"})
        if CapabilityKind.MCP in kinds:
            # Concrete mcp__server__tool names are added from the parent catalog.
            allowed.add("search_tools")
        return allowed

    def plugin_names(self) -> set[str]:
        return {
            str(item.plugin)
            for item in self.grants
            if item.kind is CapabilityKind.PLUGIN and item.plugin
        }

    def allows_action(self, action: ActionRecord) -> bool:
        if action.type in {
            ActionType.FILE_EDIT,
            ActionType.FILE_CREATE,
            ActionType.NOTEBOOK_EDIT,
        }:
            return any(
                item.kind is CapabilityKind.WORKSPACE_WRITE for item in self.grants
            )
        if action.type is ActionType.COMMAND:
            template = str(action.request.get("template", ""))
            return any(
                item.kind is CapabilityKind.COMMAND
                and (item.scope is None or item.scope == template)
                for item in self.grants
            )
        if action.type in {ActionType.WEB_SEARCH, ActionType.WEB_FETCH}:
            return self._allows_web(action)
        if action.type in {ActionType.MCP_CONNECT, ActionType.MCP_CALL}:
            plugin = action.request.get("plugin")
            if isinstance(plugin, str):
                return any(
                    item.kind is CapabilityKind.PLUGIN and item.plugin == plugin
                    for item in self.grants
                )
            server = str(action.request.get("server", ""))
            return any(
                item.kind is CapabilityKind.MCP
                and (item.scope is None or item.scope == server)
                for item in self.grants
            )
        return False

    def _allows_web(self, action: ActionRecord) -> bool:
        for item in self.grants:
            if item.kind is not CapabilityKind.WEB:
                continue
            if item.scope is None:
                return True
            if action.type is ActionType.WEB_SEARCH:
                return item.scope == "search"
            host = urlparse(str(action.request.get("url", ""))).hostname
            if host == item.scope or host and host.endswith(f".{item.scope}"):
                return True
        return False
