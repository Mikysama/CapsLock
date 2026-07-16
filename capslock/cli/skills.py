"""Interactive discovery and management of local Agent Skills."""

from __future__ import annotations

import shlex

from rich.pretty import Pretty

from ..skills import SkillValidationError
from .context import CliContext
from .render import status_text, table


def skills_command(context: CliContext, text: str) -> None:
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        context.console.print(f"[error]Invalid Skill command:[/] {exc}")
        return
    operation = parts[1].casefold() if len(parts) > 1 else "list"
    arguments = parts[2:]
    try:
        if operation == "list":
            _require_count(operation, arguments, 0)
            _list(context)
        elif operation == "show":
            _require_count(operation, arguments, 1)
            _show(context, arguments[0])
        elif operation == "validate":
            _require_count(operation, arguments, 1)
            package = context.agent.skills.get(arguments[0], require_enabled=False)
            context.console.print(
                f"[success]Skill is valid:[/] {package.name} "
                f"[text.muted]({package.scope}, {package.digest[:12]})[/]"
            )
        elif operation in {"enable", "disable"}:
            _require_count(operation, arguments, 1)
            name = arguments[0]
            context.agent.skills.get(name, require_enabled=False)
            enabled = operation == "enable"
            context.agent.store.set_skill_enabled(name, enabled)
            state = "enabled" if enabled else "disabled"
            context.console.print(f"[success]Skill {state} in this workspace:[/] {name}")
        else:
            raise ValueError(f"unsupported /skills operation: {operation}")
    except (SkillValidationError, ValueError) as exc:
        context.console.print(f"[error]Skill error:[/] {exc}")


def _list(context: CliContext) -> None:
    output = table("Skill", "Scope", "Status", "Description", "Resources")
    entries = context.agent.skills.entries()
    for entry in entries:
        package = entry.package
        description = package.description if package else (entry.error or "invalid")
        state = "enabled" if entry.enabled and not entry.error else "disabled" if package else "invalid"
        output.add_row(
            f"${entry.name}",
            entry.scope,
            status_text(state),
            description,
            str(len(package.resources) if package else 0),
        )
    if entries:
        context.console.print(output)
    else:
        context.console.print("[text.secondary]No Skills are registered.[/]")


def _show(context: CliContext, identifier: str) -> None:
    entry = next(
        (item for item in context.agent.skills.entries() if item.name == identifier),
        None,
    )
    if entry is not None:
        package = context.agent.skills.get(identifier, require_enabled=False)
        context.console.print(f"[primary.bold]${package.name}[/]")
        context.console.print(f"[text.secondary]{package.description}[/]")
        context.console.print(
            f"[text.muted]scope={package.scope} digest={package.digest[:12]} "
            f"resources={len(package.resources)} path={package.root}[/]"
        )
        if package.license:
            context.console.print(f"[text.secondary]license:[/] {package.license}")
        if package.compatibility:
            context.console.print(f"[text.secondary]compatibility:[/] {package.compatibility}")
        if package.metadata:
            context.console.print(Pretty(package.metadata))
        if package.resources:
            resources = table("Resource", "Kind", "Bytes")
            for resource in package.resources:
                resources.add_row(resource.path, resource.kind, str(resource.size))
            context.console.print(resources)
        else:
            context.console.print("[text.muted]No packaged resources.[/]")
        return
    raise ValueError(f"Skill does not exist: {identifier}")


def _require_count(operation: str, arguments: list[str], count: int) -> None:
    if len(arguments) != count:
        suffix = "argument" if count == 1 else "arguments"
        raise ValueError(f"/skills {operation} requires exactly {count} {suffix}")
