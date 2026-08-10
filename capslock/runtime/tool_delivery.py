"""Tool result delivery limits and concurrent invocation scheduling."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from ..ports import RunJournal
from ..tooling.contracts import DeliveryStatus, ExecutionContext, ToolContent


class ResultDelivery:
    def __init__(
        self,
        *,
        journal: RunJournal,
        context_factory: Callable[[str], ExecutionContext],
        aggregate_result_bytes: int,
    ) -> None:
        self.journal = journal
        self.context_factory = context_factory
        self.aggregate_result_bytes = max(1024, aggregate_result_bytes)
        self._aggregate_used = 0

    def reset(self) -> None:
        self._aggregate_used = 0

    async def enforce(self, item: Any) -> None:
        encoded = item.result_text.encode("utf-8")
        if self._aggregate_used + len(encoded) <= self.aggregate_result_bytes:
            self._aggregate_used += len(encoded)
            return
        context = self.context_factory(str(item.run_id))
        descriptor: dict[str, object]
        delivery = DeliveryStatus.DELIVERY_FAILED
        artifact_id: str | None = None
        if context.artifacts is not None and item.invocation_id is not None:
            try:
                artifact = await context.artifacts.put(
                    session_id=context.session_id,
                    run_id=str(item.run_id),
                    invocation_id=item.invocation_id,
                    content=encoded,
                )
            except Exception as exc:
                descriptor = {
                    "preview": encoded[:4096].decode("utf-8", "replace"),
                    "original_bytes": len(encoded),
                    "warning": f"aggregate artifact delivery failed: {type(exc).__name__}",
                }
            else:
                artifact_id = artifact.id
                delivery = DeliveryStatus.ARTIFACT
                descriptor = {
                    "artifact_id": artifact.id,
                    "sha256": artifact.sha256,
                    "original_bytes": len(encoded),
                    "preview": artifact.preview,
                    "read_with": "read_tool_artifact",
                    "reason": "aggregate_tool_result_budget",
                }
        else:
            descriptor = {
                "preview": encoded[:4096].decode("utf-8", "replace"),
                "original_bytes": len(encoded),
                "warning": "aggregate tool result budget exceeded",
            }
        item.outcome = replace(
            item.outcome,
            data=descriptor,
            content=(ToolContent.artifact(descriptor),) if artifact_id else (),
            delivery_status=delivery,
        )
        item.result_text = item.outcome.for_model()
        item.artifact_id = artifact_id
        self._aggregate_used += len(item.result_text.encode("utf-8"))
        if item.invocation_id is not None and hasattr(
            self.journal, "store_result_replacement"
        ):
            await self.journal.store_result_replacement(
                tool_call_id=item.call.id,
                session_id=context.session_id,
                invocation_id=item.invocation_id,
                delivery_status=delivery.value,
                replacement=json.loads(item.result_text),
            )
        if item.invocation_id is not None and hasattr(
            self.journal, "replace_tool_delivery"
        ):
            await self.journal.replace_tool_delivery(
                item.invocation_id,
                delivery_status=delivery.value,
                result_preview=item.result_text,
                artifact_id=artifact_id,
            )


class BatchScheduler:
    async def run(
        self,
        calls: tuple[Any, ...],
        *,
        prepare: Callable[[Any], Awaitable[Any]],
        commit: Callable[[Any], Awaitable[None]],
    ) -> list[Any]:
        tasks = [asyncio.create_task(prepare(call)) for call in calls]
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                if any(
                    task.result().policy.fail_fast and not task.result().ok
                    for task in done
                ):
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    pending = set()
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in tasks:
            if not task.cancelled():
                await commit(task.result())
        return [task.result() for task in tasks if not task.cancelled()]


__all__ = ["BatchScheduler", "ResultDelivery"]
