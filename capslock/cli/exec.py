"""Non-interactive workflow execution and JSONL event rendering."""

from __future__ import annotations

import asyncio
import json
import sys

from .context import CliContext


EXEC_EVENT_SCHEMA_VERSION = 1
APPROVAL_REQUIRED_EXIT = 3


def run_exec(context: CliContext, question: str | None, *, json_events: bool = False) -> int:
    prompt = question if question is not None else sys.stdin.read()
    if not prompt.strip():
        raise ValueError("exec requires a prompt argument or non-empty stdin")
    return asyncio.run(_run_exec(context, prompt, json_events=json_events))


async def _run_exec(context: CliContext, question: str, *, json_events: bool) -> int:
    waiting = False
    async for event in context.agent.ask_stream(question):
        waiting = waiting or event.kind.value == "waiting_approval"
        if json_events:
            line = json.dumps(
                {
                    "schema_version": EXEC_EVENT_SCHEMA_VERSION,
                    "sequence": event.sequence,
                    "timestamp": event.timestamp,
                    "session_id": event.session_id,
                    "run_id": event.run_id,
                    "work_item_id": event.work_item_id,
                    "event": event.kind.value,
                    "data": event.data,
                },
                ensure_ascii=False,
            )
            context.console.file.write(line + "\n")
            context.console.file.flush()
    if not json_events and context.agent.last_answer is not None:
        context.console.print(context.agent.last_answer.text, markup=False, highlight=False)
    return APPROVAL_REQUIRED_EXIT if waiting else 0
