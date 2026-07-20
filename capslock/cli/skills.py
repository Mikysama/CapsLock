"""Asynchronous local Skill command controller."""

from __future__ import annotations

import asyncio
import shlex

from ..skills import SkillValidationError
from .context import CliContext
from .views.common import status_text, table


async def skills_command(context: CliContext, text: str) -> None:
    parts = shlex.split(text)
    operation, arguments = (parts[1], parts[2:]) if len(parts) > 1 else ("list", [])
    try:
        if operation == "list":
            entries = await asyncio.to_thread(context.agent.skills.entries)
            output = table("Skill", "Scope", "Status", "Description")
            for entry in entries:
                description = (
                    entry.package.description if entry.package else entry.error or ""
                )
                status = (
                    "enabled"
                    if entry.enabled and entry.error is None
                    else "disabled"
                    if not entry.enabled
                    else "invalid"
                )
                output.add_row(
                    entry.name, entry.scope, status_text(status), description
                )
            context.console.print(output)
            return
        if len(arguments) != 1:
            raise ValueError(f"usage: /skills {operation} <name>")
        name = arguments[0]
        if operation in {"enable", "disable"}:
            enabled = operation == "enable"
            await context.agent.repositories.settings.set_skill_enabled(name, enabled)
            context.console.print(
                f"[success]{name} {'enabled' if enabled else 'disabled'}.[/]"
            )
        elif operation in {"show", "validate"}:
            package = await asyncio.to_thread(context.agent.skills.get, name)
            context.console.print(
                f"[primary.bold]{package.name}[/] [{package.scope}]\n{package.description}\n\n{package.instructions}"
            )
        else:
            raise ValueError("unknown skills command")
    except (ValueError, SkillValidationError) as exc:
        context.console.print(f"[error]Error:[/] {exc}")
