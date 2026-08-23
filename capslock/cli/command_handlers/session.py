"""Slash-command handlers for session operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ...domain import (
    AgentEventKind,
    ModelRole,
    RunKind,
)
from ...runtime.context import ContextBudgetExceeded
from ...runtime.model import ModelRunContext, open_model_session
from ...runtime.prompts import PromptSection, PromptTrust
from ...runtime.side_question import run_side_question
from ...session_management import SessionManager
from ..command_ui import Choice
from ..commands import CommandOutcome, CommandOutcomeKind
from ..presentation import present_tool
from .support import get_repositories, get_ui


async def resume(context, parts: list[str], raw: str) -> CommandOutcome:
    repositories = get_repositories(context)
    current = context.session.session_id
    if len(parts) == 1:
        candidates = [
            item for item in await repositories.sessions.list(100) if item.id != current
        ]
    else:
        query = " ".join(parts[1:]).strip()
        try:
            exact = await repositories.sessions.resolve(query)
        except ValueError:
            exact = None
        if exact is not None and exact.id != current:
            return CommandOutcome(CommandOutcomeKind.SWITCH_SESSION, exact.id)
        prefix_rows = await repositories.database.fetch_all(
            "SELECT id FROM sessions WHERE substr(id,1,?)=? AND deletion_state IS NULL ORDER BY updated_at DESC",
            (len(query), query),
        )
        if len(prefix_rows) > 1:
            candidates = [
                await repositories.sessions.require(str(row["id"]))
                for row in prefix_rows
                if str(row["id"]) != current
            ]
        else:
            candidates = [
                item
                for item in await repositories.sessions.search(query, limit=100)
                if item.id != current
            ]
    if not candidates:
        context.console.print("[text.secondary]No matching sessions.[/]")
        return CommandOutcome()
    selected = await get_ui(context).select(
        "Resume a session",
        [
            Choice(item.id, item.title, f"{item.id[:8]} · {item.updated_at}")
            for item in candidates
        ],
    )
    return (
        CommandOutcome(CommandOutcomeKind.SWITCH_SESSION, selected)
        if selected
        else CommandOutcome()
    )


async def new_session(context, parts: list[str], raw: str) -> CommandOutcome:
    if len(parts) != 1:
        raise ValueError("usage: /new")
    return CommandOutcome(CommandOutcomeKind.NEW_SESSION)


async def copy_answer(context, parts: list[str], raw: str) -> CommandOutcome:
    if len(parts) > 2:
        raise ValueError("usage: /copy [N]")
    index = int(parts[1]) if len(parts) == 2 else 1
    if index < 1:
        raise ValueError("N must be at least 1")
    answers = await context.session.sessions.assistant_answers(
        context.session.session_id
    )
    if index > len(answers):
        raise ValueError(f"assistant answer {index} does not exist")
    backend = await get_ui(context).copy(str(answers[index - 1]["content"]))
    context.console.print(f"[success]Copied assistant answer {index} via {backend}.[/]")
    return CommandOutcome()


async def export_session(context, parts: list[str], raw: str) -> CommandOutcome:
    if len(parts) > 2:
        raise ValueError("usage: /export [workspace-relative-path]")
    repositories = get_repositories(context)
    session_id = context.session.session_id
    destination = (
        parts[1]
        if len(parts) == 2
        else (
            f".capslock/exports/session-{session_id[:8]}-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        )
    )
    memory = context.application.memory_repositories if context.application else None
    target = await SessionManager(
        repositories, workspace=context.session.workspace, memory_repositories=memory
    ).export(session_id, destination)
    context.console.print(f"[success]Exported:[/] {target}")
    return CommandOutcome()


async def branch(context, parts: list[str], raw: str) -> CommandOutcome:
    parent = await context.session.sessions.require(context.session.session_id)
    title = " ".join(parts[1:]).strip() or f"{parent.title} (Branch)"
    child = await context.session.sessions.derive(
        parent.id, title=title, derivation_kind="branch"
    )
    if context.session.planning is not None:
        await context.session.planning.clone_active(
            parent.id,
            child.id,
            entry_source="branch",
            base_permission_mode=context.session.permission_mode.value,
        )
    context.console.print(f"[success]Created branch:[/] {child.title} ({child.id[:8]})")
    return CommandOutcome(CommandOutcomeKind.SWITCH_SESSION, child.id)


async def btw(context, parts: list[str], raw: str) -> CommandOutcome:
    question = " ".join(parts[1:]).strip()
    if not question:
        raise ValueError("usage: /btw <question>")
    repositories = get_repositories(context)
    audit = await repositories.runs.create_hidden(
        context.session.session_id, kind=RunKind.SIDE_QUESTION
    )
    try:
        entries = await context.session.sessions.context_entries(
            context.session.session_id
        )
        active = await repositories.compactions.active(context.session.session_id)
        preserve = context.session.context_budget.settings.preserve_recent_turns * 2
        history = entries[-preserve:] if active else entries
        bundle = await context.session._prompt_bundle()
        bundle = bundle.add(
            PromptSection(
                "runtime_control",
                "runtime:side-question",
                PromptTrust.RUNTIME_CONTROL,
                "You are answering an isolated side question. The main agent continues "
                "independently. Use the available tools when they are needed, following "
                "the normal permission policy. Do not claim to have used a tool unless its "
                "result confirms execution. Give one self-contained final answer and do not "
                "ask for a follow-up turn.",
                "Isolated side-question execution control.",
            )
        )
        if active:
            bundle = bundle.add(
                PromptSection(
                    "compaction",
                    f"compaction:{active.id}",
                    PromptTrust.UNTRUSTED_DATA,
                    json.dumps(active.summary, ensure_ascii=False, sort_keys=True),
                    "Earlier side-question session context.",
                )
            )
        messages = [
            *bundle.render(),
            *({"role": item["role"], "content": item["content"]} for item in history),
            {"role": "user", "content": question},
        ]
        if (
            context.session.context_budget.estimate(messages)
            > context.session.context_budget.input_budget
        ):
            raise ContextBudgetExceeded(
                "side-question context is too large; run /compact first"
            )
        completed_tools = {}

        async def emit(kind, data) -> None:
            if kind is AgentEventKind.TOOL_COMPLETED:
                item = present_tool(data, sequence=len(completed_tools) + 1)
                completed_tools[item.identifier] = item

        response = await run_side_question(
            context.session,
            audit.id,
            messages,
            emit=emit,
        )
        usage = await repositories.models.usage(audit.id)
        await repositories.runs.finish_hidden(
            audit.id,
            input_tokens=usage[0] or response.input_tokens,
            output_tokens=usage[1] or response.output_tokens,
            cost_usd=usage[2],
        )
        answer, _citations = await context.session.citations.resolve(
            response.text,
            evidence=response.evidence,
            source_ids=response.source_ids,
            memories=response.memories,
            session_id=context.session.session_id,
        )
        ui = get_ui(context)
        if hasattr(ui, "show_agent_response"):
            await ui.show_agent_response(
                "BTW", question, answer, tuple(completed_tools.values())
            )
        else:
            await ui.show_markdown("BTW", answer)
    except BaseException as exc:
        status = "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
        usage = await repositories.models.usage(audit.id)
        await repositories.runs.finish_hidden(
            audit.id,
            status=status,
            input_tokens=usage[0],
            output_tokens=usage[1],
            cost_usd=usage[2],
            error_message=str(exc) or type(exc).__name__,
        )
        raise
    return CommandOutcome()


async def compact(context, parts: list[str], raw: str) -> CommandOutcome:
    focus = " ".join(parts[1:]).strip() or None
    repositories = get_repositories(context)
    entries = await context.session.sessions.context_entries(context.session.session_id)
    older, recent = context.session.context_budget.split_recent(entries)
    if not older:
        context.console.print(
            "[text.secondary]Nothing to compact; there is no older history.[/]"
        )
        return CommandOutcome()
    audit = await repositories.runs.create_hidden(
        context.session.session_id, kind=RunKind.LOCAL_COMMAND
    )
    try:
        model = open_model_session(
            context.session.chat_model, ModelRunContext(audit.id, ModelRole.FAST)
        )
        record = await context.session.context_budget.compact_history(
            session_id=context.session.session_id,
            run_id=audit.id,
            entries=entries,
            summarizer=model,
            focus=focus,
        )
        usage = await repositories.models.usage(audit.id)
        await repositories.runs.finish_hidden(
            audit.id, input_tokens=usage[0], output_tokens=usage[1], cost_usd=usage[2]
        )
        context.console.print(
            f"[success]Compacted[/] {record.source_tokens} → {record.result_tokens} tokens "
            f"(target {record.target_tokens}; {record.quality_status}); "
            f"preserved {sum(item.get('role') == 'user' for item in recent) or bool(recent)} turns; "
            f"{record.id}"
        )
    except BaseException as exc:
        usage = await repositories.models.usage(audit.id)
        await repositories.runs.finish_hidden(
            audit.id,
            status="failed",
            input_tokens=usage[0],
            output_tokens=usage[1],
            cost_usd=usage[2],
            error_message=str(exc),
        )
        raise
    return CommandOutcome()
