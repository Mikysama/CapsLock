"""Slash-command handlers for context operations."""

from __future__ import annotations

import uuid

from ...storage.repositories.core import now
from ...runtime.prompts import PromptSection, PromptTrust
from ..commands import CommandOutcome
from .support import get_repositories, get_ui


async def context_info(context, parts: list[str], raw: str) -> CommandOutcome:
    if len(parts) != 1:
        raise ValueError("usage: /context")
    repositories = get_repositories(context)
    entries = await context.session.sessions.context_entries(context.session.session_id)
    active = await repositories.compactions.active(context.session.session_id)
    if active is not None and active.last_message_id is not None:
        entries = [item for item in entries if int(item["id"]) > active.last_message_id]
    manager = context.session.context_budget
    bundle = await context.session._prompt_bundle()
    if active is not None:
        bundle = bundle.add(
            PromptSection(
                "compaction",
                f"compaction:{active.id}",
                PromptTrust.UNTRUSTED_DATA,
                str(active.summary),
                "Active structured conversation summary.",
            )
        )
    messages = [
        *bundle.render(),
        *({"role": item["role"], "content": item["content"]} for item in entries),
    ]
    breakdown = manager.breakdown(messages, bundle)
    message_tokens = breakdown.history
    system_tokens = breakdown.system
    tool_tokens = breakdown.tools
    compaction_tokens = breakdown.compaction
    total = breakdown.total
    budget = context.session.context_budget.input_budget
    trigger = int(budget * context.session.context_budget.settings.trigger_ratio)
    target = context.session.context_budget.target_tokens
    live = " live stable snapshot" if context.session.engine.active else ""
    lines = [
        f"Total: {total}/{budget} tokens ({total / budget:.1%}){live}",
        f"Auto-compact threshold: {trigger} ({context.session.context_budget.settings.trigger_ratio:.0%})",
        f"Compaction target: {target} ({context.session.context_budget.settings.target_ratio:.0%})",
        "Core {core}; repository instructions {repo}; skills {skills}; memory {memory}; "
        "attachments {attachments}; compaction {compaction}; history {history}; tools {tools}".format(
            core=breakdown.core,
            repo=breakdown.repository_instructions,
            skills=breakdown.skills,
            memory=breakdown.memory,
            attachments=breakdown.attachments,
            compaction=breakdown.compaction,
            history=breakdown.history,
            tools=breakdown.tools,
        ),
        f"Estimator: {manager.estimator.strategy} ratio {manager.estimator.ratio:.3f}; "
        f"samples {manager.estimator.samples}; safety margin "
        f"{manager.estimator.safety_margin:.0%}",
    ]
    if active:
        working_set = active.summary.get("working_set", [])
        lines.append(
            f"Active compaction: {active.id}; source {active.source_tokens}; "
            f"result {active.result_tokens}; target {active.target_tokens}; "
            f"quality {active.quality_status}; working set {len(working_set) if isinstance(working_set, list) else 0}; "
            f"{active.created_at}"
        )
    if context.session.context_budget.last_no_progress_reason:
        lines.append(
            "No progress: " + context.session.context_budget.last_no_progress_reason
        )
    lines.append(
        "Last micro-compaction saving: "
        f"{context.session.context_budget.last_micro_compaction_saved_tokens} tokens"
    )
    await get_ui(context).show("Context", "\n".join(lines))
    await repositories.database.execute(
        """INSERT INTO context_snapshots(id,session_id,compaction_id,system_tokens,tool_tokens,
           message_tokens,memory_tokens,compaction_tokens,total_tokens,input_budget,trigger_tokens,stable,created_at)
           VALUES(?,?,?,?,?,?,0,?,?,?,?,1,?)""",
        (
            f"ctx_{uuid.uuid4().hex}",
            context.session.session_id,
            active.id if active else None,
            system_tokens,
            tool_tokens,
            message_tokens,
            compaction_tokens,
            total,
            budget,
            trigger,
            now(),
        ),
    )
    return CommandOutcome()
