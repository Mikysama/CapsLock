"""Typed slash-command catalog shared by both interactive frontends."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import CliContext


class CommandAvailability(StrEnum):
    ALWAYS = "always"
    IDLE_ONLY = "idle_only"
    IMMEDIATE = "immediate"


class CommandOutcomeKind(StrEnum):
    HANDLED = "handled"
    EXIT = "exit"
    SWITCH_SESSION = "switch_session"
    NEW_SESSION = "new_session"
    ENQUEUE = "enqueue"


@dataclass(frozen=True)
class CommandOutcome:
    kind: CommandOutcomeKind = CommandOutcomeKind.HANDLED
    session_id: str | None = None
    work_item_id: str | None = None
    question: str | None = None


CommandHandler = Callable[["CliContext", list[str], str], Awaitable[CommandOutcome]]
_HANDLERS: dict[str, CommandHandler] = {}


async def _invoke_registered(
    context: "CliContext", parts: list[str], raw: str
) -> CommandOutcome:
    path = parts[0]
    try:
        handler = _HANDLERS[path]
    except KeyError:
        raise RuntimeError(f"command handler is not registered: {path}") from None
    return await handler(context, parts, raw)


@dataclass(frozen=True)
class CommandSpec:
    path: str
    description: str
    usage: str = ""
    group: str = "core"
    availability: CommandAvailability = CommandAvailability.ALWAYS
    handler: CommandHandler | None = None


def _spec(
    path: str,
    description: str,
    usage: str = "",
    group: str = "core",
    availability: CommandAvailability = CommandAvailability.ALWAYS,
) -> CommandSpec:
    return CommandSpec(
        path, description, usage or path, group, availability, _invoke_registered
    )


COMMANDS = (
    _spec("/help", "Show commands"),
    _spec(
        "/plan",
        "Enter, inspect, edit, submit, or exit Plan Mode",
        "/plan [goal|show|open|submit|exit]",
        "session",
        CommandAvailability.IDLE_ONLY,
    ),
    _spec(
        "/init",
        "Analyze the repository and propose root CAPSLOCK.md",
        group="workspace",
        availability=CommandAvailability.IDLE_ONLY,
    ),
    _spec("/status", "Show session, plan, queue, context, and usage"),
    _spec(
        "/resume",
        "Switch to a saved session",
        "/resume [session-id-prefix|query]",
        "session",
        CommandAvailability.IDLE_ONLY,
    ),
    _spec(
        "/btw",
        "Ask a temporary side question",
        "/btw <question>",
        "session",
        CommandAvailability.IMMEDIATE,
    ),
    _spec(
        "/compact",
        "Compact older session history",
        "/compact [focus instructions]",
        "session",
        CommandAvailability.IDLE_ONLY,
    ),
    _spec(
        "/new",
        "Start a new session",
        group="session",
        availability=CommandAvailability.IDLE_ONLY,
    ),
    _spec("/copy", "Copy a recent assistant answer", "/copy [N]", "session"),
    _spec(
        "/export",
        "Export this session",
        "/export [workspace-relative-path]",
        "session",
        CommandAvailability.IDLE_ONLY,
    ),
    _spec(
        "/branch",
        "Branch this conversation",
        "/branch [title]",
        "session",
        CommandAvailability.IDLE_ONLY,
    ),
    _spec("/context", "Inspect the stable context budget", group="inspect"),
    _spec(
        "/worktree",
        "Manage session worktrees",
        "/worktree [list|create <name>|exit [keep|remove] [--discard]]",
        "workspace",
        CommandAvailability.IDLE_ONLY,
    ),
    _spec(
        "/rewind",
        "Branch from an earlier run and safely restore files",
        "/rewind [run-id-prefix]",
        "session",
        CommandAvailability.IDLE_ONLY,
    ),
    _spec(
        "/stats",
        "Show workspace or session statistics",
        "/stats [workspace|session]",
        "inspect",
    ),
    _spec("/doctor", "Run read-only diagnostics", "/doctor [--network]", "inspect"),
    _spec("/model", "Show or change the model"),
    _spec("/permissions", "Show or change permission mode"),
    _spec("/approvals", "Review, approve, or reject pending actions"),
    _spec("/queue", "List, move, cancel, or retry foreground work"),
    _spec("/memory", "Manage local memory"),
    _spec("/instructions", "Inspect loaded repository instructions"),
    _spec("/skills", "Manage local Skills"),
    _spec("/agents", "Inspect, cancel, or clean local child Agents"),
    _spec("/sources", "List external sources", group="inspect"),
    _spec("/mcp", "Inspect MCP servers", group="inspect"),
    _spec("/diff", "Show Git diff", group="inspect"),
    _spec(
        "/undo",
        "Undo the last CapsLock file action",
        availability=CommandAvailability.IDLE_ONLY,
    ),
    _spec("/rename", "Rename this session", "/rename <title>"),
    _spec("/exit", "Exit CapsLock"),
    _spec("/quit", "Exit CapsLock"),
)


def register_handler(path: str, handler: CommandHandler) -> None:
    if path not in {item.path for item in COMMANDS}:
        raise ValueError(f"cannot register unknown command: {path}")
    _HANDLERS[path] = handler


def command_descriptions() -> dict[str, str]:
    return {item.path: item.description for item in COMMANDS}


def command_completions(prefix: str) -> list[str]:
    return [item.path for item in COMMANDS if item.path.startswith(prefix)]


def command_menu_completions(prefix: str) -> list[str]:
    exact = next((item.path for item in COMMANDS if item.path == prefix), None)
    return [exact] if exact else command_completions(prefix)


def resolve_command(text: str) -> CommandSpec | None:
    name = text.split(maxsplit=1)[0]
    item = next((item for item in COMMANDS if item.path == name), None)
    return item
