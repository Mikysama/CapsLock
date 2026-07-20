"""CapsLock v2 slash-command catalog."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    path: str
    description: str


COMMANDS = (
    CommandSpec("/help", "Show commands"),
    CommandSpec("/status", "Show session, plan, queue, context, and usage"),
    CommandSpec("/permissions", "Show or change permission mode"),
    CommandSpec("/approvals", "Review, approve, or reject pending actions"),
    CommandSpec("/queue", "List, move, cancel, or retry foreground work"),
    CommandSpec("/memory", "Manage local memory"),
    CommandSpec("/skills", "Manage local Skills"),
    CommandSpec("/sources", "List external sources"),
    CommandSpec("/mcp", "Inspect MCP servers"),
    CommandSpec("/diff", "Show Git diff"),
    CommandSpec("/undo", "Undo the last CapsLock file action"),
    CommandSpec("/rename", "Rename this session"),
    CommandSpec("/exit", "Exit CapsLock"),
    CommandSpec("/quit", "Exit CapsLock"),
)


def command_descriptions() -> dict[str, str]:
    return {item.path: item.description for item in COMMANDS}


def command_completions(prefix: str) -> list[str]:
    return [item.path for item in COMMANDS if item.path.startswith(prefix)]


def command_menu_completions(prefix: str) -> list[str]:
    return command_completions(prefix)


def resolve_command(text: str) -> CommandSpec | None:
    name = text.split(maxsplit=1)[0]
    return next((item for item in COMMANDS if item.path == name), None)
