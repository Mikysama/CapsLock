"""Async approval-gated action subsystem."""

from .commands import CommandActionHandler, CommandTemplate, TEMPLATES
from .core import (
    ActionCoordinator,
    ActionExecution,
    ActionHandler,
    ActionProposal,
    ActionRunState,
)
from .credentials import CredentialActionHandler, resolve_named_credential
from .external import McpActionHandler, WebActionHandler
from .files import FileActionHandler
from .worktrees import WorkspaceExecutionScope, WorktreeActionHandler
from .session import SessionRewindActionHandler

__all__ = [
    "ActionCoordinator",
    "ActionExecution",
    "ActionHandler",
    "ActionProposal",
    "ActionRunState",
    "CommandActionHandler",
    "CommandTemplate",
    "CredentialActionHandler",
    "FileActionHandler",
    "McpActionHandler",
    "TEMPLATES",
    "WebActionHandler",
    "WorkspaceExecutionScope",
    "WorktreeActionHandler",
    "SessionRewindActionHandler",
    "resolve_named_credential",
]
