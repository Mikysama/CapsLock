"""Async approval-gated action subsystem."""

from .commands import CommandActionHandler, CommandTemplate, TEMPLATES
from .core import ActionCoordinator, ActionExecution, ActionHandler, ActionProposal
from .external import McpActionHandler, WebActionHandler
from .files import FileActionHandler

__all__ = [
    "ActionCoordinator",
    "ActionExecution",
    "ActionHandler",
    "ActionProposal",
    "CommandActionHandler",
    "CommandTemplate",
    "FileActionHandler",
    "McpActionHandler",
    "TEMPLATES",
    "WebActionHandler",
]
