"""Single application entry point for approval-gated actions."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..changes import ChangeService
from ..config import CommandSettings, McpSettings, WebSettings
from ..domain import ActionInfo, ActionStatus, ActionType, ChangeInfo, CommandInfo, ExternalActionInfo
from ..execution import CommandService
from ..external import WebService
from ..layout import ProjectLayout
from ..mcp import McpService
from ..permissions import ApprovalPolicy, PermissionMode
from ..policy import PolicyError, WorkspacePolicy
from ..session import SessionStore


ActionRecord = ChangeInfo | CommandInfo | ExternalActionInfo


class ActionCoordinator:
    """Coordinate proposal, policy, approval, execution, and reversal."""

    def __init__(
        self,
        *,
        store: SessionStore,
        policy: WorkspacePolicy,
        session_id: str,
        run_id: str,
        event: Callable[..., None],
        permission_mode: PermissionMode = PermissionMode.APPROVE_FOR_ME,
        command: CommandSettings | None = None,
        web: WebSettings | None = None,
        mcp: McpSettings | None = None,
        layout: ProjectLayout | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.session_id = session_id
        self.run_id = run_id
        self.event = event
        self.permission_mode = permission_mode
        self.command_config = command or CommandSettings(120, 100_000)
        self.web_config = web or WebSettings(None, 20, 500_000, 3)
        self.mcp_config = mcp or McpSettings(30, 100_000)
        self.layout = layout or ProjectLayout.discover(policy.root)
        self.approvals = ApprovalPolicy()

    def for_run(self, run_id: str) -> "ActionCoordinator":
        return ActionCoordinator(
            store=self.store,
            policy=self.policy,
            session_id=self.session_id,
            run_id=run_id,
            event=self.event,
            permission_mode=self.permission_mode,
            command=self.command_config,
            web=self.web_config,
            mcp=self.mcp_config,
            layout=self.layout,
        )

    def propose(self, action_type: ActionType, **payload: Any) -> ActionRecord:
        if action_type is ActionType.FILE_EDIT:
            record = self._changes().propose_edit(payload["path"], payload["old_text"], payload["new_text"], payload.get("summary", ""))
        elif action_type is ActionType.FILE_CREATE:
            record = self._changes().propose_create(payload["path"], payload["content"], payload.get("summary", ""))
        elif action_type is ActionType.COMMAND:
            record = self._commands().propose(payload["template"], target=payload.get("target"), cwd=payload.get("cwd", "."))
        elif action_type is ActionType.WEB_SEARCH:
            service = self._web()
            try:
                record = service.propose_search(payload["query"])
            finally:
                service.close()
        elif action_type is ActionType.WEB_FETCH:
            service = self._web()
            try:
                record = service.propose_fetch(payload["url"])
            finally:
                service.close()
        elif action_type is ActionType.MCP_CONNECT:
            record = self._mcp().propose_connect(payload["server"])
        elif action_type is ActionType.MCP_CALL:
            record = self._mcp().propose_call(payload["server"], payload["tool"], payload["arguments"])
        else:  # pragma: no cover - exhaustive StrEnum guard
            raise ValueError(f"unsupported action type: {action_type}")
        return self._apply_policy(action_type, record)

    def approve_and_execute(self, action_id: str) -> ActionRecord:
        action = self.resolve(action_id)
        if action.type in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
            service = self._changes(action.run_id)
            service.approve(action.id)
            return service.apply(action.id)
        if action.type is ActionType.COMMAND:
            service = self._commands(action.run_id)
            service.approve(action.id)
            return service.execute(action.id)
        service = self._external_service(action)
        try:
            service.actions.approve(action.id)
            return service.execute(action.id)
        finally:
            if isinstance(service, WebService):
                service.close()

    async def approve_and_execute_async(self, action_id: str) -> ActionRecord:
        action = self.resolve(action_id)
        if action.type not in {ActionType.MCP_CONNECT, ActionType.MCP_CALL}:
            return self.approve_and_execute(action_id)
        service = self._mcp(action.run_id)
        service.actions.approve(action.id)
        return await service.execute_async(action.id)

    def execute_auto_approved(self, action_id: str) -> ActionRecord:
        action = self.resolve(action_id)
        if self._is_skill_change(action):
            raise ValueError("project Skill changes require explicit approval")
        assessment = self.approvals.assess(action.type)
        self.event("risk_assessed", action=action.type.value, level=assessment.level, rollback=assessment.rollback)
        self.event("auto_approved", action=action.type.value, level=assessment.level)
        return self.approve_and_execute(action.id)

    def execute_approved(self, action_id: str) -> ActionRecord:
        action = self.resolve(action_id)
        if action.status is not ActionStatus.APPROVED:
            raise ValueError("action requires explicit approval before execution")
        if action.type in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
            return self._changes(action.run_id).apply(action.id)
        if action.type is ActionType.COMMAND:
            return self._commands(action.run_id).execute(action.id)
        service = self._external_service(action)
        try:
            return service.execute(action.id)
        finally:
            if isinstance(service, WebService):
                service.close()

    def reject(self, action_id: str) -> ActionRecord:
        action = self.resolve(action_id)
        if action.type in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
            return self._changes(action.run_id).reject(action.id)
        if action.type is ActionType.COMMAND:
            return self._commands(action.run_id).reject(action.id)
        service = self._external_service(action)
        try:
            return service.actions.reject(action.id)
        finally:
            if isinstance(service, WebService):
                service.close()

    def reverse_last_file_action(self) -> ChangeInfo:
        return self._changes().undo_last()

    def resolve(self, prefix: str, *, types: set[ActionType] | None = None) -> ActionInfo:
        action = self.store.resolve_action(self.session_id, prefix, types=types)
        if action is None:
            raise PolicyError("action does not belong to this session or does not exist")
        return action

    def _apply_policy(self, action_type: ActionType, record: ActionRecord) -> ActionRecord:
        assessment = self.approvals.assess(action_type)
        self.store.set_action_risk(
            record.id,
            level=assessment.level,
            reason=assessment.reason,
            rollback=assessment.rollback,
        )
        record = self._record(record.id, action_type)
        self.event("risk_assessed", action=action_type.value, level=assessment.level, rollback=assessment.rollback)
        if self._is_skill_change(record) or self.approvals.requires_approval(self.permission_mode, action_type):
            return record
        if action_type in {ActionType.MCP_CONNECT, ActionType.MCP_CALL}:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                # The synchronous tool protocol cannot nest MCP's async client in the TUI loop.
                # Keep the action visibly pending; the approval center executes it asynchronously.
                return record
        if self.permission_mode is PermissionMode.FULL_ACCESS:
            self.event("auto_approved", action=action_type.value, level=assessment.level)
        return self.approve_and_execute(record.id)

    def _record(self, action_id: str, action_type: ActionType) -> ActionRecord:
        if action_type in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
            result = self.store.get_change(action_id, session_id=self.session_id)
        elif action_type is ActionType.COMMAND:
            result = self.store.get_command(action_id, session_id=self.session_id)
        else:
            result = self.store.get_external_action(action_id, session_id=self.session_id)
        if result is None:
            raise ValueError(f"action does not exist: {action_id}")
        return result

    def _is_skill_change(self, record: ActionInfo | ActionRecord) -> bool:
        if isinstance(record, ActionInfo):
            action_type = record.type
        elif isinstance(record, ChangeInfo):
            action_type = ActionType.FILE_CREATE if record.operation == "create" else ActionType.FILE_EDIT
        else:
            return False
        if action_type not in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
            return False
        path = getattr(record, "path", None)
        if path is None:
            change = self.store.get_change(record.id, session_id=self.session_id)
            path = change.path if change is not None else None
        parts = path.split("/") if path is not None else []
        return len(parts) >= 3 and tuple(parts[:2]) == (".capslock", "skills")

    def _changes(self, run_id: str | None = None) -> ChangeService:
        return ChangeService(self.store, self.policy, self.session_id, run_id or self.run_id, self.event)

    def _commands(self, run_id: str | None = None) -> CommandService:
        config = self.command_config
        return CommandService(self.store, self.policy, self.session_id, run_id or self.run_id, self.event, timeout_seconds=config.command_timeout_seconds, output_limit_bytes=config.command_output_bytes)

    def _web(self, run_id: str | None = None) -> WebService:
        config = self.web_config
        return WebService(self.store, self.session_id, run_id or self.run_id, self.event, tavily_api_key=config.tavily_api_key, timeout_seconds=config.web_timeout_seconds, max_bytes=config.web_max_bytes, max_redirects=config.web_max_redirects)

    def _mcp(self, run_id: str | None = None) -> McpService:
        config = self.mcp_config
        return McpService(self.store, self.policy, self.session_id, run_id or self.run_id, self.event, timeout_seconds=config.mcp_timeout_seconds, output_limit_bytes=config.mcp_output_bytes, layout=self.layout)

    def _external_service(self, action: ActionInfo) -> WebService | McpService:
        return self._mcp(action.run_id) if action.type in {ActionType.MCP_CONNECT, ActionType.MCP_CALL} else self._web(action.run_id)
