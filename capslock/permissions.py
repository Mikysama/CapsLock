"""User-selectable approval policy and explicit risk/rollback guidance."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .domain import ActionType


class PermissionMode(StrEnum):
    FULL_ACCESS = "full_access"
    APPROVE_FOR_ME = "approve_for_me"
    ASK_FOR_APPROVAL = "ask_for_approval"

    @classmethod
    def parse(cls, value: str) -> "PermissionMode":
        aliases = {"full": cls.FULL_ACCESS, "approve": cls.APPROVE_FOR_ME, "ask": cls.ASK_FOR_APPROVAL}
        normalized = value.casefold()
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError("permission mode must be full_access, approve_for_me, or ask_for_approval") from exc


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    reason: str
    rollback: str


class ApprovalPolicy:
    def assess(self, action: ActionType) -> RiskAssessment:
        if action in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
            return RiskAssessment("high", "Modifies workspace files.", "CapsLock records original content; use /undo while the file is unchanged.")
        if action is ActionType.COMMAND:
            return RiskAssessment("high", "Runs a local process that may execute project code.", "Process groups are timeout/cancel bounded; inspect /diff for workspace changes.")
        if action in {ActionType.MCP_CONNECT, ActionType.MCP_CALL}:
            return RiskAssessment("high", "Starts an external local server or invokes a third-party tool.", "The stdio server is short-lived; CapsLock cannot reverse third-party side effects.")
        if action in {ActionType.WEB_SEARCH, ActionType.WEB_FETCH}:
            return RiskAssessment("medium", "Sends a query or URL to an external network service.", "No local mutation; results and sources remain auditable.")
        return RiskAssessment("low", "Reads local/session state only.", "No mutation is performed.")

    def requires_approval(self, mode: PermissionMode, action: ActionType) -> bool:
        assessment = self.assess(action)
        if mode is PermissionMode.FULL_ACCESS:
            return False
        if mode is PermissionMode.ASK_FOR_APPROVAL:
            return True
        return assessment.level == "high"


_LEGACY_ACTIONS = {
    "propose_file_edit": ActionType.FILE_EDIT,
    "propose_file_create": ActionType.FILE_CREATE,
    "apply_change": ActionType.FILE_EDIT,
    "propose_command": ActionType.COMMAND,
    "run_command": ActionType.COMMAND,
    "propose_mcp_connect": ActionType.MCP_CONNECT,
    "propose_mcp_call": ActionType.MCP_CALL,
    "propose_web_search": ActionType.WEB_SEARCH,
    "propose_web_fetch": ActionType.WEB_FETCH,
}


def assess(action: str) -> RiskAssessment:
    mapped = _LEGACY_ACTIONS.get(action)
    if mapped is not None:
        return ApprovalPolicy().assess(mapped)
    if action in {"propose_file_edit", "propose_file_create", "apply_change"}:
        return RiskAssessment("high", "Modifies workspace files.", "CapsLock records original content; use /undo while the file is unchanged.")
    if action in {"propose_command", "run_command"}:
        return RiskAssessment("high", "Runs a local process that may execute project code.", "Process groups are timeout/cancel bounded; inspect /diff for workspace changes.")
    if action.startswith("propose_mcp"):
        return RiskAssessment("high", "Starts an external local server or invokes a third-party tool.", "The stdio server is short-lived; CapsLock cannot reverse third-party side effects.")
    if action.startswith("propose_web"):
        return RiskAssessment("medium", "Sends a query or URL to an external network service.", "No local mutation; results and sources remain auditable.")
    return RiskAssessment("low", "Reads local/session state only.", "No mutation is performed.")


def requires_approval(mode: PermissionMode, assessment: RiskAssessment) -> bool:
    if mode is PermissionMode.FULL_ACCESS:
        return False
    if mode is PermissionMode.ASK_FOR_APPROVAL:
        return True
    return assessment.level == "high"
