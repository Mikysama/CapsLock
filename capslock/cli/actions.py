"""Async CLI controllers for approvals, permissions, sources, MCP, diff, and undo."""

from __future__ import annotations

import asyncio
import json
import shlex

from ..domain import ActionRecord, ActionStatus, RunKind
from ..layout import ProjectLayout
from ..mcp import McpRegistry
from ..permissions import PermissionMode
from .context import CliContext
from .views.actions import render_approvals as render_approval_view
from .views.actions import render_sources as render_source_view
from .prompt import (
    select_model,
    select_permission_mode,
    select_permission_request_decision,
)


async def render_approvals(context: CliContext) -> None:
    items = await context.require_queries().actions(
        context.session.session_id,
        statuses={ActionStatus.PENDING, ActionStatus.APPROVED, ActionStatus.RUNNING},
    )
    render_approval_view(context.console, items)
    for request in await context.session.permission_requests(status="pending"):
        context.console.print(
            f"[warning]{str(request['id'])[:12]}[/] permission "
            f"{request['tool']}: {request['reason']}"
        )
    for request in await context.session.plan_requests():
        context.console.print(
            f"[warning]{request.id[:12]}[/] plan {request.kind.value}: "
            f"{request.objective or request.plan_id or '-'}"
        )


async def render_sources(context: CliContext) -> None:
    render_source_view(
        context.console,
        await context.require_queries().sources(context.session.session_id),
    )


async def approve_action(context: CliContext, prefix: str):
    coordinator = context.session.action_factory("cli")
    try:
        try:
            action = await coordinator.resolve(prefix)
        except ValueError:
            try:
                plan_request = await context.session.resolve_plan_request(prefix)
            except ValueError:
                plan_request = None
            if plan_request is not None:
                from .plans import decide_plan_request_interactively

                await decide_plan_request_interactively(context, plan_request)
                if plan_request.run_id:
                    return await _resume_paused_action(
                        context, plan_request.run_id
                    )
                return None
            request = await context.session.resolve_permission_request(prefix)
            decision = await asyncio.to_thread(
                select_permission_request_decision, request
            )
            await context.session.decide_permission_request(
                str(request["id"]), decision
            )
            await _resume_paused_action(context, str(request["run_id"]))
            context.console.print(
                f"[success]Permission decided:[/] {str(request['id'])[:12]}"
            )
            return
        context.console.print(
            f"[warning]Approve {action.type.value}:[/] {action.summary}\n{action.request}"
        )
        answer = await asyncio.to_thread(
            context.console.input, "Approve and execute? [y/N] "
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            context.console.print("[waiting]Action remains pending.[/]")
            return
        result = await coordinator.for_run(action.run_id).approve_and_execute(action.id)
        context.console.print(
            f"[success]{result.status.value}:[/] {result.id[:12]} {result.result or ''}"
        )
        await context.session.workflow.settle_approval(
            context.session.session_id, action.run_id
        )
        await _resume_paused_action(context, action.run_id)
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def reject_action(context: CliContext, prefix: str):
    coordinator = context.session.action_factory("cli")
    try:
        try:
            action = await coordinator.resolve(prefix)
        except ValueError:
            try:
                plan_request = await context.session.resolve_plan_request(prefix)
            except ValueError:
                plan_request = None
            if plan_request is not None:
                await context.session.decide_plan_request(
                    plan_request.id, "reject"
                )
                if plan_request.run_id:
                    await _resume_paused_action(context, plan_request.run_id)
                context.console.print(
                    f"[warning]Rejected plan request:[/] {plan_request.id[:12]}"
                )
                return
            request = await context.session.resolve_permission_request(prefix)
            await context.session.decide_permission_request(
                str(request["id"]), "reject"
            )
            await _resume_paused_action(context, str(request["run_id"]))
            context.console.print(
                f"[warning]Rejected permission request:[/] {str(request['id'])[:12]}"
            )
            return
        await coordinator.for_run(action.run_id).reject(action.id)
        await context.session.workflow.settle_approval(
            context.session.session_id, action.run_id
        )
        await _resume_paused_action(context, action.run_id)
        context.console.print(f"[warning]Rejected {action.type.value}.[/]")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def apply_action_decision(
    context: CliContext, action: ActionRecord, decision: str
) -> None:
    coordinator = context.session.action_factory("cli").for_run(action.run_id)
    if decision == "later":
        context.console.print(f"[waiting]Action remains pending:[/] {action.id[:12]}")
        return
    if decision == "reject":
        await coordinator.reject(action.id)
        await context.session.workflow.settle_approval(
            context.session.session_id, action.run_id
        )
        await _resume_paused_action(context, action.run_id)
        context.console.print(
            f"[warning]Rejected {action.type.value}:[/] {action.id[:12]}"
        )
        return
    if decision not in {"approve", "approve_once", "approve_session", "approve_local"}:
        raise ValueError(f"unsupported action decision: {decision}")
    result = await coordinator.approve_with_choice(action.id, decision)
    context.console.print(
        f"[success]{result.status.value}:[/] {result.id[:12]} {result.result or ''}"
    )
    await context.session.workflow.settle_approval(
        context.session.session_id, action.run_id
    )
    await _resume_paused_action(context, action.run_id)


async def _resume_paused_action(context: CliContext, run_id: str):
    run = await context.session.runs.require(
        run_id, session_id=context.session.session_id
    )
    if run.status != "waiting_approval":
        return None
    final_run_id = None
    async for event in context.session.resume_paused_stream(run_id):
        final_run_id = event.run_id
        if event.kind.value == "text_delta":
            context.console.print(str(event.data.get("text", "")), end="")
    if run.kind is RunKind.INIT:
        bundle = await asyncio.to_thread(
            context.session.instruction_loader.load, context.session.workspace
        )
        context.console.print(
            f"\n[success]Reloaded repository instructions:[/] {bundle.digest[:12]}"
        )
    implementation_loader = getattr(
        context.session, "implementation_for_planning_run", None
    )
    if final_run_id is not None and callable(implementation_loader):
        return await implementation_loader(final_run_id)
    return None


async def undo(context: CliContext) -> None:
    try:
        action = await context.session.action_factory("cli").reverse_last_file_action()
        context.console.print(f"[success]Undone:[/] {action.request.get('path')}")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def set_permission_mode(context: CliContext, value: str) -> None:
    try:
        mode = PermissionMode.parse(value)
        await context.session.persist_permission_mode(mode)
        context.console.print(f"[success]Permission mode:[/] {mode.value}")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def permissions(context: CliContext, text: str) -> None:
    parts = shlex.split(text)
    if len(parts) == 1:
        try:
            selected = await asyncio.to_thread(
                select_permission_mode, context.session.permission_mode
            )
        except (EOFError, KeyboardInterrupt):
            context.console.print("[waiting]Permission mode unchanged.[/]")
            return
        await set_permission_mode(context, selected.value)
        return
    if len(parts) == 2 and parts[1] == "rules":
        rules = await context.session.permission_rules()
        if not rules:
            context.console.print("[text.secondary]No permission rules.[/]")
        for rule in rules:
            context.console.print(
                f"[command]{rule.get('source')}[/] {rule.get('behavior')} "
                f"{rule.get('tool')} id={rule.get('id') or '-'} "
                f"constraints={json.dumps(rule.get('constraints', {}), ensure_ascii=False)}"
            )
        return
    if len(parts) in {2, 3} and parts[1] == "recent":
        limit = int(parts[2]) if len(parts) == 3 else 20
        for item in await context.session.recent_permission_decisions(limit=limit):
            context.console.print(
                f"{item['created_at']} [command]{item['tool']}[/] "
                f"{item['behavior']} source={item['source']} "
                f"reason={item['reason_code']} digest={item['arguments_sha256'][:12]}"
            )
        return
    if len(parts) == 2 and parts[1] == "doctor":
        diagnostics = await context.session.permission_diagnostics()
        if not diagnostics:
            context.console.print("[success]Permission rules are valid.[/]")
        for message in diagnostics:
            context.console.print(f"[warning]{message}[/]")
        return
    if len(parts) == 2 and parts[1] == "trust-project":
        answer = await asyncio.to_thread(
            context.console.input,
            "Trust allow rules in the current project permissions file? [y/N] ",
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            context.console.print("[waiting]Project permissions remain untrusted.[/]")
            return
        digest = await context.session.trust_project_permissions()
        context.console.print(
            f"[success]Trusted project permissions:[/] sha256={digest}"
        )
        return
    if len(parts) in {5, 6} and parts[1] == "add":
        destination, behavior, tool = parts[2:5]
        constraints = json.loads(parts[5]) if len(parts) == 6 else {}
        if not isinstance(constraints, dict):
            raise ValueError("permission constraints must be a JSON object")
        if destination in {"user", "project"} or "*" in json.dumps(constraints):
            answer = await asyncio.to_thread(
                context.console.input,
                "This rule has a broad or shared scope. Add it? [y/N] ",
            )
            if answer.strip().casefold() not in {"y", "yes"}:
                context.console.print("[waiting]Permission rule was not added.[/]")
                return
        identifier = await context.session.apply_permission_update(
            {
                "operation": "add",
                "destination": destination,
                "behavior": behavior,
                "tool": tool,
                "constraints": constraints,
            }
        )
        context.console.print(f"[success]Added permission rule:[/] {identifier}")
        return
    if len(parts) == 4 and parts[1] == "remove":
        destination, identifier = parts[2:4]
        if destination in {"user", "project"}:
            answer = await asyncio.to_thread(
                context.console.input,
                "Remove this shared permission rule? [y/N] ",
            )
            if answer.strip().casefold() not in {"y", "yes"}:
                context.console.print("[waiting]Permission rule was not removed.[/]")
                return
        await context.session.apply_permission_update(
            {
                "operation": "remove",
                "destination": destination,
                "rule_id": identifier,
            }
        )
        context.console.print(f"[success]Removed permission rule:[/] {identifier}")
        return
    if len(parts) != 2:
        context.console.print(
            "[error]Usage:[/] /permissions [full|approve|ask|rules|recent [n]|doctor|trust-project|"
            "add <session|local|project|user> <allow|ask|deny> <tool> [constraints-json]|"
            "remove <scope> <id>]"
        )
        return
    await set_permission_mode(context, parts[1])


async def set_model(context: CliContext, value: str) -> None:
    try:
        model = await context.session.set_model(value)
        context.console.print(f"[success]Model:[/] {model}")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def model_command(context: CliContext, text: str) -> None:
    parts = shlex.split(text)
    if len(parts) == 1:
        try:
            selected = await asyncio.to_thread(select_model, context.session.model)
        except (EOFError, KeyboardInterrupt):
            context.console.print("[waiting]Model unchanged.[/]")
            return
        await set_model(context, selected)
        return
    if len(parts) != 2:
        context.console.print(
            "[error]Usage:[/] /model [deepseek-v4-flash|deepseek-v4-pro]"
        )
        return
    await set_model(context, parts[1])


async def mcp_command(context: CliContext, text: str) -> None:
    parts = shlex.split(text)
    registry = McpRegistry(
        context.session.policy, layout=ProjectLayout.discover(context.session.workspace)
    )
    try:
        if len(parts) == 1 or parts[1] == "list":
            servers = await asyncio.to_thread(registry.servers)
            for server in servers.values():
                context.console.print(
                    f"{server.name} scope={server.scope} enabled={server.enabled} tools={','.join(server.allowed_tools)}"
                )
        elif len(parts) == 3 and parts[1] in {"status", "tools"}:
            server = await asyncio.to_thread(registry.get, parts[2])
            context.console.print(
                f"{server.name} cwd={server.cwd} enabled={server.enabled} tools={','.join(server.allowed_tools)}"
            )
        else:
            raise ValueError("usage: /mcp [list|status <server>|tools <server>]")
    except ValueError as exc:
        context.console.print(f"[error]Error:[/] {exc}")


async def show_git_diff(context: CliContext) -> None:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(context.session.workspace),
        "diff",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    output = stdout if process.returncode == 0 else stderr
    context.console.print(
        output.decode("utf-8", "replace"), markup=False, highlight=False
    )
