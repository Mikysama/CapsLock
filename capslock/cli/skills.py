"""Interactive management and explicit execution of local Skills."""

from __future__ import annotations

import json
import shlex
from typing import Any

from rich.pretty import Pretty

from ..runtime import AgentRuntimeError, SkillAnswer
from ..skills import SkillPackage, SkillValidationError
from . import actions
from .context import CliContext
from .render import status_text, table


def skills_command(context: CliContext, text: str) -> None:
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        context.console.print(f"[error]Invalid Skill command:[/] {exc}")
        return
    if len(parts) < 2:
        _list(context)
        return
    operation = parts[1].casefold()
    arguments = parts[2:]
    try:
        if operation == "list":
            _list(context)
        elif operation == "show":
            _require_count(operation, arguments, 1)
            _show(context, arguments[0])
        elif operation == "validate":
            _require_count(operation, arguments, 1)
            package = context.agent.skills.get(arguments[0], require_enabled=False)
            context.console.print(
                f"[success]Skill is valid:[/] {package.name} v{package.manifest.version} "
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
        elif operation == "run":
            if not arguments:
                raise ValueError("/skills run requires a Skill name")
            _run(context, arguments[0], arguments[1:])
        else:
            raise ValueError(f"unsupported /skills operation: {operation}")
    except (AgentRuntimeError, SkillValidationError, ValueError) as exc:
        context.console.print(f"[error]Skill error:[/] {exc}")


def parse_skill_input(package: SkillPackage, arguments: list[str], input_value) -> dict[str, Any]:
    properties = package.input_schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    output: dict[str, Any] = {}
    for argument in arguments:
        key, separator, raw = argument.partition("=")
        if not separator or not key:
            raise ValueError(f"Skill input must use key=value: {argument}")
        if key in output:
            raise ValueError(f"duplicate Skill input: {key}")
        output[key] = _coerce(raw, properties.get(key))
    required = package.input_schema.get("required", [])
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in output:
                raw = input_value(f"{key}> ")
                output[key] = _coerce(raw, properties.get(key))
    package.validate_input(output)
    return output


def _coerce(raw: str, schema: object) -> Any:
    expected = schema.get("type") if isinstance(schema, dict) else None
    if expected == "string" or expected is None:
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"input for type {expected} must be valid JSON: {raw}") from exc


def _list(context: CliContext) -> None:
    output = table("Skill", "Version", "Scope", "Status", "Description")
    entries = context.agent.skills.entries()
    for entry in entries:
        package = entry.package
        version = package.manifest.version if package else "-"
        description = package.manifest.description if package else (entry.error or "invalid")
        state = "enabled" if entry.enabled and not entry.error else "disabled" if package else "invalid"
        output.add_row(entry.name, version, entry.scope, status_text(state), description)
    if entries:
        context.console.print(output)
    else:
        context.console.print("[text.secondary]No Skills are registered.[/]")


def _show(context: CliContext, identifier: str) -> None:
    package = None
    try:
        package = context.agent.skills.get(identifier, require_enabled=False)
    except SkillValidationError:
        pass
    if package is not None:
        manifest = package.manifest
        context.console.print(f"[primary.bold]{package.name}[/] v{manifest.version}")
        context.console.print(f"[text.secondary]{manifest.description}[/]")
        context.console.print(
            f"[text.muted]scope={package.scope} min_capslock={manifest.min_capslock_version} "
            f"digest={package.digest[:12]}[/]"
        )
        context.console.print(f"[text.secondary]tools:[/] {', '.join(manifest.required_tools) or '(none)'}")
        context.console.print(
            f"[text.secondary]permissions:[/] {', '.join(manifest.required_permissions) or '(none)'}"
        )
        return
    run = context.agent.store.resolve_skill_run(identifier, session_id=context.agent.session_id)
    if run is None:
        historical = context.agent.store.list_skill_runs(context.agent.session_id, name=identifier)
        run = historical[0] if historical else None
    if run is None:
        raise ValueError(f"Skill or Skill run does not exist: {identifier}")
    context.console.print(f"[primary.bold]{run.name}[/] v{run.version} · {status_text(run.status)}")
    context.console.print(
        f"[text.muted]run={run.run_id} scope={run.scope} digest={run.manifest_digest[:12]} "
        f"created={run.created_at} finished={run.finished_at or '-'}[/]"
    )
    context.console.print(Pretty({"input": run.input, "output": run.output, "error": run.error}))


def _run(context: CliContext, name: str, arguments: list[str]) -> None:
    package = context.agent.skills.get(name)
    input_data = parse_skill_input(package, arguments, context.console.input)
    with context.console.status(f"[running.bold]Running Skill {name}...[/]"):
        answer = context.agent.run_skill(name, input_data)
    _render_answer(context, answer)
    actions.render_changes(context, pending_only=True)
    actions.review_pending_external_actions(context, answer.run_id)


def _render_answer(context: CliContext, answer: SkillAnswer) -> None:
    context.console.print(
        f"\n[agent.bold]Skill {answer.skill_name}[/] [text.muted]v{answer.skill_version}[/]"
    )
    context.console.print(Pretty(answer.output))
    for item in answer.citations:
        if hasattr(item, "path"):
            context.console.print(
                f"  [text.secondary]Evidence:[/] [path]{item.path}[/]:L{item.start_line}-L{item.end_line}"
            )
        elif hasattr(item, "url"):
            context.console.print(f"  [text.secondary]Source:[/] {item.title} · {item.url}")
        else:
            context.console.print(f"  [text.secondary]Memory:[/] {item.id}")
    context.console.print(f"  [text.muted]Run {answer.run_id[:8]} · {answer.duration_ms}ms[/]")


def _require_count(operation: str, arguments: list[str], count: int) -> None:
    if len(arguments) != count:
        raise ValueError(f"/skills {operation} requires exactly {count} argument")
