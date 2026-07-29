"""Restricted repository-instruction initialization command."""

from __future__ import annotations

from ...domain import RunKind
from ..commands import CommandOutcome, CommandOutcomeKind


INIT_REQUEST = """Analyze this repository and prepare its root CAPSLOCK.md.
Inspect README files, language/package manifests, build and test configuration, CI configuration, and existing AI instruction files. Record only commands, architecture constraints, and project conventions that a capable model cannot reliably infer from the repository itself.
The only permitted write target is the repository-root CAPSLOCK.md. If it does not exist, propose a concise new file. If it exists, read it first and use its SHA-256 precondition to make the smallest useful edit. If only AGENTS.md exists, use it as compatibility input but do not modify it or copy it wholesale; add only CapsLock-specific gaps.
Do not create any other instruction file. The write always requires explicit user Action approval. If the repository does not provide enough evidence for an important choice, ask the user before proposing the file."""


async def initialize(context, parts: list[str], raw: str) -> CommandOutcome:
    del raw
    if len(parts) != 1:
        raise ValueError("usage: /init")
    item = await context.session.enqueue(INIT_REQUEST, kind=RunKind.INIT)
    context.console.print(
        f"[success]Queued restricted repository initialization:[/] {item.id[:12]}"
    )
    return CommandOutcome(
        CommandOutcomeKind.ENQUEUE,
        work_item_id=item.id,
        question=item.question,
    )


__all__ = ["INIT_REQUEST", "initialize"]
