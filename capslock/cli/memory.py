"""Interactive adapters for explicit local-memory management."""

from __future__ import annotations

import shlex

from ..domain import MemoryScope, MemoryType
from ..policy import PolicyError
from .context import CliContext
from .render import render_memories, render_memory, render_memory_policy, select_choice


def memory_command(context: CliContext, text: str) -> None:
    memory = getattr(context.agent, "memory", None)
    if memory is None:
        context.console.print("[error]Error:[/] memory storage is unavailable")
        return
    try:
        parts = shlex.split(text)
        operation = parts[1] if len(parts) > 1 else "status"
        arguments = parts[2:]
        if operation == "status":
            render_memory_policy(context.console, memory)
        elif operation == "enable":
            memory.set_local_write_enabled(True)
            render_memory_policy(context.console, memory)
        elif operation == "disable":
            memory.set_local_write_enabled(False)
            render_memory_policy(context.console, memory)
        elif operation == "list":
            _list(context, arguments)
        elif operation == "search":
            if not arguments:
                raise ValueError("Usage: /memory search <query>")
            render_memories(context.console, memory.search(" ".join(arguments)), title="Memory search")
        elif operation == "show":
            render_memory(context.console, memory.resolve(_one(arguments, "show")))
        elif operation == "add":
            _add(context)
        elif operation == "edit":
            _edit(context, _one(arguments, "edit"))
        elif operation == "forget":
            item = memory.forget(_one(arguments, "forget"))
            context.console.print(f"[success]Forgotten:[/] [text.muted]{item.id}[/] · use /memory undo to restore")
        elif operation == "undo":
            item = memory.undo(_one(arguments, "undo"))
            context.console.print(f"[success]Memory operation undone:[/] [text.muted]{item.id}[/] · {item.status.value}")
        elif operation == "purge":
            _purge(context, _one(arguments, "purge"))
        elif operation == "export":
            _export(context, arguments)
        elif operation == "import":
            _import(context, arguments)
        else:
            raise ValueError("unknown /memory command; use /memory status or /help")
    except (FileExistsError, PermissionError, PolicyError, ValueError) as exc:
        context.console.print(f"[error]Error:[/] {exc}")


def _list(context: CliContext, arguments: list[str]) -> None:
    include_inactive = "--all" in arguments
    values = [item for item in arguments if item != "--all"]
    if len(values) > 1:
        raise ValueError("Usage: /memory list [global|workspace|session] [--all]")
    scope = MemoryScope(values[0]) if values else None
    render_memories(
        context.console,
        context.agent.memory.list(scope=scope, include_inactive=include_inactive),
        title="Memories",
    )


def _add(context: CliContext) -> None:
    memory = context.agent.memory
    scope_choice = select_choice(
        context.console,
        "Memory scope",
        (("global", "Global"), ("workspace", "Current workspace"), ("session", "Current session"), ("cancel", "Cancel")),
        default=1,
    )
    if scope_choice == "cancel":
        return
    scope = MemoryScope(scope_choice)
    memory_type = MemoryType(select_choice(
        context.console,
        "Memory type",
        tuple((item.value, item.value) for item in MemoryType),
    ))
    content = context.console.input("Memory content> ")
    confidence = _number(context.console.input("Confidence 0-1 [1]> "), default=1.0)
    expires = context.console.input("Expires at RFC 3339 [never]> ").strip() or None
    item, rules = memory.add(
        content=content, memory_type=memory_type, scope=scope, confidence=confidence, expires_at=expires
    )
    _saved(context, item, rules)


def _edit(context: CliContext, prefix: str) -> None:
    memory = context.agent.memory
    current = memory.resolve(prefix)
    render_memory(context.console, current)
    type_value = context.console.input(f"Type [{current.type.value}]> ").strip() or current.type.value
    content = context.console.input("Replacement content (blank keeps current)> ") or current.content or ""
    confidence = _number(
        context.console.input(f"Confidence 0-1 [{current.confidence:g}]> "), default=current.confidence
    )
    expiry_default = current.expires_at or "never"
    expiry_text = context.console.input(f"Expires at RFC 3339 [{expiry_default}]> ").strip()
    expires = current.expires_at if not expiry_text else (None if expiry_text.casefold() == "never" else expiry_text)
    item, rules = memory.edit(
        current.id,
        content=content,
        memory_type=MemoryType(type_value),
        confidence=confidence,
        expires_at=expires,
    )
    _saved(context, item, rules)


def _purge(context: CliContext, prefix: str) -> None:
    item = context.agent.memory.resolve(prefix)
    render_memory(context.console, item)
    if context.console.input("Permanently purge content and history? [y/N] ").strip().casefold() not in {"y", "yes"}:
        context.console.print("[waiting]Purge cancelled.[/]")
        return
    purged = context.agent.memory.purge(item.id)
    context.console.print(f"[success]Purged permanently:[/] [text.muted]{purged.id}[/]")


def _export(context: CliContext, arguments: list[str]) -> None:
    if len(arguments) != 2:
        raise ValueError("Usage: /memory export <global|workspace|session> <relative.json>")
    scope, path = MemoryScope(arguments[0]), arguments[1]
    try:
        output, count = context.agent.memory.export_json(scope, path)
    except FileExistsError:
        if context.console.input("Export file exists. Overwrite? [y/N] ").strip().casefold() not in {"y", "yes"}:
            context.console.print("[waiting]Export cancelled.[/]")
            return
        output, count = context.agent.memory.export_json(scope, path, overwrite=True)
    context.console.print(f"[success]Exported {count} memories:[/] [path]{output}[/]")


def _import(context: CliContext, arguments: list[str]) -> None:
    if len(arguments) != 2:
        raise ValueError("Usage: /memory import <global|workspace|session> <relative.json>")
    scope, path = MemoryScope(arguments[0]), arguments[1]
    items, rules = context.agent.memory.import_json(scope, path)
    context.console.print(f"[success]Imported {len(items)} memories into {scope.value} scope.[/]")
    _rules(context, rules)


def _saved(context: CliContext, item: object, rules: tuple[str, ...]) -> None:
    context.console.print(
        f"[success]Saved memory:[/] [text.muted]{item.id}[/] · {item.type.value} · {item.scope.value} · r{item.revision}"
    )
    _rules(context, rules)


def _rules(context: CliContext, rules: tuple[str, ...]) -> None:
    if rules:
        context.console.print(f"[warning]Sensitive content was redacted by: {', '.join(rules)}[/]")


def _one(arguments: list[str], operation: str) -> str:
    if len(arguments) != 1:
        raise ValueError(f"Usage: /memory {operation} <id>")
    return arguments[0]


def _number(value: str, *, default: float) -> float:
    return default if not value.strip() else float(value)
