"""Shared command-handler accessors."""

from ..command_ui import ConsoleCommandUI


def get_ui(context):
    return context.ui or ConsoleCommandUI(context.console)


def get_repositories(context):
    if context.application is not None:
        return context.application.repositories
    raise RuntimeError("this command requires an application command context")


__all__ = ["get_repositories", "get_ui"]
