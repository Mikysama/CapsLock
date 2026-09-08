"""Isolated agentic execution for ephemeral ``/btw`` questions."""

from __future__ import annotations

from typing import Any

from ..domain import ModelRole, RunStepKind, RunStepStatus
from .model import ModelRunContext, open_model_session
from .tool_loop import ToolLoop, ToolLoopResult


class _MetadataOnlyJournal:
    """Keep tool/run audit metadata without persisting side-question prose."""

    def __init__(self, journal: Any) -> None:
        self._journal = journal

    async def create_step(self, run_id: str, kind: RunStepKind) -> Any:
        return await self._journal.create_step(run_id, kind)

    async def update_tool_invocation(self, identifier: str, **values: Any) -> None:
        await self._journal.update_tool_invocation(identifier, **values)

    async def finish_step(
        self,
        step_id: str,
        *,
        status: RunStepStatus,
        checkpoint: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Any:
        return await self._journal.finish_step(
            step_id, status=status, checkpoint=None, error=error
        )

    async def start_tool_invocation(self, **values: Any) -> str:
        return await self._journal.start_tool_invocation(**{**values, "arguments": {}})

    async def finish_tool_invocation(self, identifier: str, **values: Any) -> None:
        await self._journal.finish_tool_invocation(
            identifier, **{**values, "result_preview": ""}
        )

    async def record_tool_call(
        self,
        run_id: str,
        name: str,
        arguments: dict[str, Any],
        ok: bool,
        summary: str,
        duration_ms: int,
        *,
        invocation_id: str,
    ) -> None:
        await self._journal.record_tool_call(
            run_id, name, {}, ok, "", duration_ms, invocation_id=invocation_id
        )

    async def store_result_replacement(self, **values: Any) -> None:
        await self._journal.store_result_replacement(
            **{**values, "replacement": {"redacted": True}}
        )

    async def replace_tool_delivery(self, identifier: str, **values: Any) -> None:
        await self._journal.replace_tool_delivery(
            identifier,
            **{**values, "result_preview": "", "artifact_id": None},
        )

    async def pause_tool_invocation(self, identifier: str, **values: Any) -> None:
        await self._journal.pause_tool_invocation(
            identifier, **{**values, "continuation": {}}
        )

    async def pause_step(self, identifier: str, **values: Any) -> None:
        await self._journal.pause_step(identifier, **{**values, "checkpoint": {}})

    async def update_step_checkpoint(
        self, identifier: str, checkpoint: dict[str, Any]
    ) -> None:
        await self._journal.update_step_checkpoint(identifier, {})

    async def create_input_request(self, **values: Any) -> None:
        await self._journal.create_input_request(
            **{**values, "questions": [], "resume_data": {}}
        )


async def run_side_question(
    session: Any,
    run_id: str,
    messages: list[dict[str, object]],
    *,
    emit: Any,
) -> ToolLoopResult:
    """Run a forked FAST agent with its own tool-loop state and event sink."""

    model_session = open_model_session(
        session.chat_model, ModelRunContext(run_id, ModelRole.FAST)
    )
    journal = _MetadataOnlyJournal(session.journal)
    loop = ToolLoop(
        chat_model=session.chat_model,
        model=session.model,
        tools=session.tools,
        journal=journal,
        max_tool_rounds=session.max_tool_rounds,
        context_factory=lambda active_run_id: session._run_context(
            active_run_id,
            model_session=model_session,
            artifacts=None,
        ),
    )
    return await loop.run(
        messages,
        run_id,
        emit=emit,
        chat_model=model_session,
    )
