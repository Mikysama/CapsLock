"""Async slash-command routing."""

from __future__ import annotations

import json
import shlex

from . import actions
from .commands import COMMANDS, resolve_command
from .context import CliContext
from .memory import memory_command
from .skills import skills_command
from .views.workflow import StatusView, render_queue, render_status


async def dispatch_slash_command(context: CliContext, text: str) -> str:
    spec = resolve_command(text)
    if spec is None:
        context.console.print("[warning]Unknown command. Use /help.[/]")
        return "handled"
    parts = shlex.split(text)
    name = spec.path
    if name in {"/exit", "/quit"}:
        return "exit"
    if name == "/help":
        for item in COMMANDS:
            context.console.print(f"[command]{item.path:<14}[/] {item.description}")
    elif name == "/status":
        await _status(context)
    elif name == "/model":
        await actions.model_command(context, text)
    elif name == "/permissions":
        await actions.permissions(context, text)
    elif name == "/approvals":
        if len(parts) == 3 and parts[1] == "approve":
            await actions.approve_action(context, parts[2])
        elif len(parts) == 3 and parts[1] == "reject":
            await actions.reject_action(context, parts[2])
        else:
            await actions.render_approvals(context)
            context.console.print(
                "[text.secondary]Use /approvals approve <id> or /approvals reject <id>.[/]"
            )
    elif name == "/queue":
        await _queue(context, parts)
    elif name == "/memory":
        await memory_command(context, text)
    elif name == "/skills":
        await skills_command(context, text)
    elif name == "/agents":
        await _agents(context, parts)
    elif name == "/sources":
        await actions.render_sources(context)
    elif name == "/mcp":
        await actions.mcp_command(context, text)
    elif name == "/diff":
        await actions.show_git_diff(context)
    elif name == "/undo":
        await actions.undo(context)
    elif name == "/rename":
        if len(parts) < 2:
            context.console.print("[error]Usage:[/] /rename <title>")
        else:
            session = await context.session.rename(" ".join(parts[1:]))
            context.console.print(f"[success]Renamed:[/] {session.title}")
    return "handled"


async def _status(context: CliContext) -> None:
    agent = context.session
    queries = context.require_queries()
    session = await queries.session(agent.session_id)
    tasks = await queries.tasks(agent.session_id)
    work = await queries.work_items(agent.session_id, active_only=True)
    cost = await queries.session_cost(agent.session_id)
    count = await queries.message_count(agent.session_id)
    latest_budget = await queries.latest_budget(agent.session_id)
    render_status(
        context.console,
        StatusView(
            session,
            str(agent.workspace),
            agent.model,
            agent.permission_mode.value,
            tasks,
            work,
            *cost,
            count,
            agent.context_budget.input_budget,
            latest_budget.as_dict() if latest_budget else None,
        ),
    )
    collaboration = getattr(agent, "collaboration", None)
    if collaboration is not None:
        children = await queries.collaboration_tasks(agent.session_id)
        active = sum(
            item["state"] in {"created", "running", "waiting_approval"}
            for item in children
        )
        failed = sum(
            item["state"] in {"failed", "cancelled", "interrupted"} for item in children
        )
        waiting = sum(item["state"] == "waiting_approval" for item in children)
        usage = await _collaboration_usage(queries, children)
        context.console.print(
            f"[text.secondary]Child Agents:[/] {len(children)} total, "
            f"{active}/{collaboration.max_concurrency} active, {waiting} waiting approval, "
            f"{failed} failed/interrupted; {usage['tokens']} tokens, "
            f"{usage['tool_rounds']} rounds, ${usage['cost_usd']:.6f}"
        )


async def _agents(context: CliContext, parts: list[str]) -> None:
    collaboration = getattr(context.session, "collaboration", None)
    if collaboration is None:
        context.console.print("[warning]Multi-Agent collaboration is disabled.[/]")
        return
    queries = context.require_queries()
    if len(parts) == 1:
        items = await queries.collaboration_tasks(context.session.session_id)
        if not items:
            context.console.print("[text.secondary]No child Agent tasks.[/]")
            return
        active = sum(
            item["state"] in {"created", "running", "waiting_approval"}
            for item in items
        )
        usage = await _collaboration_usage(queries, items)
        context.console.print(
            f"[text.secondary]Concurrency {active}/{collaboration.max_concurrency}; "
            f"budget used {usage['tokens']} tokens, {usage['tool_rounds']} rounds, "
            f"${usage['cost_usd']:.6f}[/]"
        )
        for item in items:
            output = await queries.collaboration_output(str(item["id"]))
            verification = (
                "verified"
                if output is not None and output.verified
                else "unverified"
                if output is not None
                else "pending"
            )
            context.console.print(
                f"[command]{str(item['id'])[:8]}[/] {item['state']} "
                f"({verification}): {item['objective']}"
            )
        return
    if len(parts) == 3 and parts[1] == "inspect":
        item = await queries.collaboration_task(parts[2])
        if item is None:
            raise ValueError("child task does not exist")
        context.console.print(f"[command]Task:[/] {item['id']}")
        context.console.print(f"[command]State:[/] {item['state']}")
        context.console.print(f"[command]Objective:[/] {item['objective']}")
        contract = json.loads(str(item["contract_json"]))
        context.console.print(
            f"[command]Capabilities:[/] {json.dumps(contract.get('capabilities', []), ensure_ascii=False)}"
        )
        context.console.print(
            f"[command]Allowed paths:[/] {json.dumps(contract.get('allowed_paths', []), ensure_ascii=False)}"
        )
        context.console.print(
            f"[command]Limits:[/] {json.dumps(contract.get('limits', {}), ensure_ascii=False)}"
        )
        workspace = await queries.collaboration_workspace(str(item["id"]))
        if workspace is not None:
            context.console.print(
                f"[command]Workspace:[/] {workspace['path']} "
                f"retained={bool(workspace['retained'])} cleaned={bool(workspace['cleaned_at'])}"
            )
        if item.get("child_run_id"):
            context.console.print(f"[command]Child run:[/] {item['child_run_id']}")
        if item.get("error"):
            context.console.print(f"[error]Failure:[/] {item['error']}")
        for message in await queries.collaboration_messages(str(item["id"])):
            context.console.print(
                f"[text.secondary]{message['sequence']} {message['message_kind']} {message['payload_sha256'][:12]}[/]"
            )
        output = await queries.collaboration_output(str(item["id"]))
        if output is not None:
            context.console.print(
                f"[command]Verified:[/] {output.verified} {output.summary or output.error or ''}"
            )
        return
    if len(parts) == 3 and parts[1] == "cancel":
        await collaboration.cancel(parts[2])
        return
    if len(parts) == 3 and parts[1] == "cleanup":
        await collaboration.cleanup(parts[2])
        return
    raise ValueError("usage: /agents [inspect|cancel|cleanup <task-id>]")


async def _collaboration_usage(queries, items: list[dict]) -> dict[str, float | int]:
    usage: dict[str, float | int] = {
        "tokens": 0,
        "tool_rounds": 0,
        "cost_usd": 0.0,
    }
    for item in items:
        output = await queries.collaboration_output(str(item["id"]))
        if output is None:
            continue
        usage["tokens"] += int(output.usage.get("input_tokens", 0)) + int(
            output.usage.get("output_tokens", 0)
        )
        usage["tool_rounds"] += int(output.usage.get("tool_rounds", 0))
        usage["cost_usd"] += float(output.usage.get("cost_usd", 0))
    return usage


async def _queue(context: CliContext, parts: list[str]) -> None:
    queries = context.require_queries()
    if len(parts) == 1:
        render_queue(
            context.console,
            await queries.work_items(context.session.session_id, active_only=True),
        )
        return
    if len(parts) == 3 and parts[1] == "cancel":
        await context.session.cancel_queued_work_item(parts[2])
        return
    if len(parts) == 4 and parts[1] == "move":
        await context.session.reorder_queued_work_item(parts[2], int(parts[3]))
        return
    if len(parts) == 3 and parts[1] == "retry":
        context.console.print(
            "[text.secondary]Retry is queued by the active TUI worker.[/]"
        )
        return
    if len(parts) == 3 and parts[1] == "start":
        context.console.print(
            "[text.secondary]Queued work is started by the active TUI worker.[/]"
        )
        return
    raise ValueError(
        "usage: /queue [start <id>|cancel <id>|move <id> <position>|retry <run-id>]"
    )
