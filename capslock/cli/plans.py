"""Plan Mode slash-command workflow."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from pathlib import Path

from ..planning import MAX_PLAN_BYTES
from .command_ui import ConsoleCommandUI
from .commands import CommandOutcome, CommandOutcomeKind


def _ui(context):
    return context.ui or ConsoleCommandUI(context.console)


async def plan_command(context, parts: list[str], raw: str) -> CommandOutcome:
    del raw
    service = context.session.planning
    if service is None:
        raise ValueError("planning service is unavailable")
    operation = parts[1] if len(parts) > 1 else None
    if operation == "show":
        if len(parts) != 2:
            raise ValueError("usage: /plan show")
        return await _show(context)
    if operation == "open":
        if len(parts) != 2:
            raise ValueError("usage: /plan open")
        await _open(context)
        return await _show(context)
    if operation == "submit":
        if len(parts) != 2:
            raise ValueError("usage: /plan submit")
        return await _submit(context)
    if operation == "exit":
        if len(parts) != 2:
            raise ValueError("usage: /plan exit")
        current = await service.current(context.session.session_id)
        if current is None:
            context.console.print("[text.secondary]Plan Mode is not active.[/]")
            return CommandOutcome()
        confirmed = await _ui(context).confirm(
            "Exit Plan Mode?",
            "The draft remains in session history, but no implementation will run.",
        )
        if confirmed:
            await service.repository.cancel(current[0].id)
            context.console.print("[warning]Plan Mode exited.[/]")
        return CommandOutcome()

    objective = " ".join(parts[1:]).strip()
    current = await service.current(context.session.session_id)
    if current is None:
        if objective:
            await service.create(
                context.session.session_id,
                objective,
                entry_source="slash",
                base_permission_mode=context.session.permission_mode.value,
            )
        else:
            latest = await service.repository.latest_unapproved(
                context.session.session_id
            )
            if latest is None:
                await service.create(
                    context.session.session_id,
                    "Plan the requested work",
                    entry_source="slash",
                    base_permission_mode=context.session.permission_mode.value,
                )
            elif latest.status.value in {"draft", "awaiting_approval"}:
                current_revision = await service.repository.current_revision(latest)
                await service.sync_mirror(latest, current_revision)
            else:
                revision = await service.repository.current_revision(latest)
                await service.create(
                    context.session.session_id,
                    latest.objective,
                    entry_source="resume",
                    base_permission_mode=context.session.permission_mode.value,
                    parent_plan_id=latest.id,
                    content=revision.content,
                    revision_source="branch",
                )
        current = await service.current(context.session.session_id)
        assert current is not None
        context.console.print("[success]Enabled plan mode[/]")
        context.console.print(
            "[text.secondary]CapsLock can explore the workspace and update the plan. "
            "No implementation changes will be made until you approve it.[/]"
        )
    if not objective:
        return await _show(context)
    item = await context.session.enqueue(objective)
    return CommandOutcome(
        CommandOutcomeKind.ENQUEUE,
        work_item_id=item.id,
        question=item.question,
    )


async def _show(context) -> CommandOutcome:
    current = await context.session.planning.current(context.session.session_id)
    if current is None:
        context.console.print("[text.secondary]Plan Mode is not active.[/]")
        return CommandOutcome()
    plan, revision = current
    await _ui(context).show_markdown(
        f"Current plan · {plan.status.value} · r{revision.ordinal} · {revision.sha256[:12]}",
        revision.content,
    )
    return CommandOutcome()


async def _open(context) -> None:
    service = context.session.planning
    current = await service.current(context.session.session_id)
    if current is None:
        raise ValueError("Plan Mode is not active")
    plan, revision = current
    path = await service.sync_mirror(plan, revision)
    configured = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    command = shlex.split(configured) if configured else []
    if not command:
        editor = shutil.which("vi")
        if editor is None:
            raise ValueError("set VISUAL or EDITOR to use /plan open")
        command = [editor]
    process = await asyncio.create_subprocess_exec(*command, str(path))
    if await process.wait() != 0:
        raise ValueError("plan editor exited unsuccessfully")
    path = service.path(plan)
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError("plan exceeds the 256 KiB limit")
    try:
        content = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
    except UnicodeError as exc:
        raise ValueError("plan editor produced invalid UTF-8") from exc
    await service.update(
        context.session.session_id,
        content,
        expected_sha256=revision.sha256,
        source="editor",
    )


async def _submit(context) -> CommandOutcome:
    service = context.session.planning
    current = await service.current(context.session.session_id)
    if current is None:
        raise ValueError("Plan Mode is not active")
    plan, revision = current
    request = await service.repository.submit(
        plan.id,
        expected_sha256=revision.sha256,
        run_id=None,
        invocation_id=None,
    )
    result = await _ui(context).request_plan_approval(
        objective=plan.objective,
        content=revision.content,
        revision=revision.ordinal,
        sha256=revision.sha256,
        permission_mode=context.session.permission_mode.value,
    )
    selected, feedback = result.choice, result.feedback
    decided = await context.session.decide_plan_request(
        request.id, selected, feedback=feedback
    )
    if selected == "implement":
        implementation = await service.repository.ensure_implementation(decided.id)
        item = await context.session.work_items.require(
            implementation.work_item_id
        )
        return CommandOutcome(
            CommandOutcomeKind.ENQUEUE,
            work_item_id=item.id,
            question=item.question,
        )
    if selected == "feedback" and feedback:
        item = await context.session.enqueue(feedback)
        return CommandOutcome(
            CommandOutcomeKind.ENQUEUE,
            work_item_id=item.id,
            question=item.question,
        )
    return CommandOutcome()


__all__ = ["plan_command"]


async def decide_plan_request_interactively(context, request) -> str:
    ui = _ui(context)
    if request.kind.value == "enter":
        approved = await ui.request_plan_entry(
            request.objective or "Plan the requested work"
        )
        choice, feedback = ("enter" if approved else "reject"), None
    else:
        if request.plan_id is None or request.revision_id is None:
            raise ValueError("plan submission request is incomplete")
        plan = await context.session.planning.repository.require(request.plan_id)
        revision = await context.session.planning.repository.require_revision(
            request.revision_id
        )
        result = await ui.request_plan_approval(
            objective=plan.objective,
            content=revision.content,
            revision=revision.ordinal,
            sha256=revision.sha256,
            permission_mode=context.session.permission_mode.value,
        )
        choice, feedback = result.choice, result.feedback
    await context.session.decide_plan_request(
        request.id, choice, feedback=feedback
    )
    return choice


__all__ = ["decide_plan_request_interactively", "plan_command"]
