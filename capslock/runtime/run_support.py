"""Run orchestration, event publication, and terminal accounting helpers."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..domain import (
    AgentEvent,
    AgentEventKind,
    LoopDetectionSettings,
    RunLimits,
    RunMode,
    WorkItemStatus,
)
from ..ports import ModelAuditPort, RunJournal, WorkflowPort, WorkspaceServicesPort
from ..security import redact
from .governance import RunGovernor
from .model import ChatModel, ModelRunContext, ModelRunSession, open_model_session


@dataclass(frozen=True)
class ActiveRun:
    prepared: Any
    governor: RunGovernor
    model_session: ModelRunSession
    started: float

    @property
    def run_id(self) -> str:
        return str(self.prepared.run.id)


class RunOrchestrator:
    def __init__(
        self,
        *,
        services: WorkspaceServicesPort,
        workflow: WorkflowPort,
        chat_model: ChatModel,
        default_limits: RunLimits,
        loop_settings: LoopDetectionSettings,
    ) -> None:
        self.services = services
        self.workflow = workflow
        self.chat_model = chat_model
        self.default_limits = default_limits
        self.loop_settings = loop_settings

    async def start(
        self,
        session_id: str,
        question: str,
        *,
        work_item_id: str | None,
        resume_from_run_id: str | None,
        mode: RunMode,
        limits: RunLimits | None,
    ) -> ActiveRun:
        prepared = await self.workflow.prepare(
            session_id,
            question,
            work_item_id=work_item_id,
            resume_from_run_id=resume_from_run_id,
        )
        governor = await RunGovernor.create(
            self.services,
            prepared.run.id,
            parent_run_id=prepared.run.parent_run_id,
            mode=mode,
            limits=limits or self.default_limits,
            loop_settings=self.loop_settings,
        )
        snapshot = governor.snapshot
        model_session = open_model_session(
            self.chat_model,
            ModelRunContext(
                prepared.run.id,
                limits=snapshot.limits,
                budget_base=(snapshot.tokens, snapshot.cost_usd),
                hard_budget=mode is RunMode.EXEC,
            ),
        )
        return ActiveRun(prepared, governor, model_session, time.monotonic())


class RunEventPublisher:
    def __init__(
        self,
        *,
        run_id: str,
        journal: RunJournal,
        event: Callable[..., None],
        consumer: Callable[[AgentEvent], Awaitable[None]],
    ) -> None:
        self.run_id = run_id
        self.journal = journal
        self.event = event
        self.consumer = consumer

    async def publish(self, event: AgentEvent) -> None:
        self.event(
            "workflow_event",
            run_id=event.run_id,
            work_item_id=event.work_item_id,
            event=event.kind.value,
            data=event.data,
        )
        await self.consumer(event)

    async def emit(self, kind: AgentEventKind, data: dict[str, Any]) -> None:
        await self.publish(
            await self.journal.append_event(self.run_id, kind, redact(data))
        )


@dataclass(frozen=True)
class RunUsage:
    input_tokens: int
    output_tokens: int
    cost_usd: float
    models: list[dict[str, Any]]


class RunFinalizer:
    def __init__(
        self,
        *,
        workflow: WorkflowPort,
        journal: RunJournal,
        model_audit: ModelAuditPort,
        input_cost_per_million: float,
        output_cost_per_million: float,
    ) -> None:
        self.workflow = workflow
        self.journal = journal
        self.model_audit = model_audit
        self.input_cost = input_cost_per_million
        self.output_cost = output_cost_per_million

    async def usage(
        self,
        run_id: str,
        model_session: ModelRunSession,
        input_tokens: int,
        output_tokens: int,
    ) -> RunUsage:
        models = await model_session.summary()
        if model_session.metered:
            input_tokens, output_tokens, cost = await self.model_audit.usage(run_id)
        else:
            cost = (
                input_tokens * self.input_cost + output_tokens * self.output_cost
            ) / 1_000_000
        return RunUsage(input_tokens, output_tokens, cost, models)

    async def fail_if_running(
        self,
        *,
        run_id: str,
        started: float,
        status: WorkItemStatus,
        kind: AgentEventKind,
        error_code: str,
        message: str,
        input_tokens: int,
        output_tokens: int,
        model_session: ModelRunSession,
    ) -> AgentEvent | None:
        run = await self.journal.get_run(run_id)
        if run is None or run.status != WorkItemStatus.RUNNING.value:
            return None
        usage = await self.usage(run_id, model_session, input_tokens, output_tokens)
        duration = round((time.monotonic() - started) * 1000)
        return await self.workflow.finish(
            run_id,
            status=status,
            event_kind=kind,
            payload={
                "status": status.value,
                "error": {"code": error_code, "message": message},
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cost_usd": usage.cost_usd,
                },
                "models": usage.models,
            },
            duration_ms=duration,
            error_code=error_code,
            error_message=message,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        )
