"""Tool policy resolution, middleware and invocation pipeline."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from .catalog import ToolCatalog, ToolCatalogSnapshot
from .contracts import (
    ExecutionContext,
    InterruptBehavior,
    ResolvedToolPolicy,
    ToolDefinition,
    ToolEvent,
    ToolEventKind,
    ToolExecution,
    ToolInvocationResult,
    ToolMiddleware,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
    ToolReporter,
    null_reporter,
)
from .schema import SchemaValidationError, compile_json_schema


class ToolExecutor:
    """Execute definitions from one catalog through an ordered middleware chain."""

    def __init__(
        self, catalog: ToolCatalog, middleware: Iterable[ToolMiddleware] = ()
    ) -> None:
        self.catalog = catalog
        self.middleware = tuple(middleware)

    async def resolve(
        self, name: str, context: ExecutionContext, arguments: dict[str, Any]
    ) -> ResolvedToolPolicy:
        tool = self.catalog._tools.get(name)
        if tool is None:
            raise SchemaValidationError(f"unsupported tool: {name}")
        compile_json_schema(tool.contract.input_schema).validate(arguments)
        await tool.validate(arguments, context)
        return await tool.resolve_policy(arguments, context)

    async def invoke(
        self,
        name: str,
        context: ExecutionContext,
        arguments: dict[str, Any],
        reporter: ToolReporter = null_reporter,
    ) -> ToolInvocationResult:
        tool = self.catalog._tools.get(name)
        if tool is None:
            return ToolInvocationResult(
                ToolOutcome.failure(
                    f"unsupported tool: {name}", code="unsupported_tool"
                ),
                arguments,
                ResolvedToolPolicy(),
                {},
            )
        normalized = dict(arguments)
        timings: dict[str, int] = {}
        policy = ResolvedToolPolicy()
        context.event("tool_started", name=name)
        started = time.monotonic()
        try:
            phase = time.monotonic()
            await reporter(ToolEvent(ToolEventKind.PHASE, {"phase": "validating"}))
            for item in self.middleware:
                normalized = await item.normalize(tool, normalized, context)
            compile_json_schema(tool.contract.input_schema).validate(normalized)
            await tool.validate(normalized, context)
            policy = await tool.resolve_policy(normalized, context)
            timings["validation"] = round((time.monotonic() - phase) * 1000)

            phase = time.monotonic()
            await reporter(ToolEvent(ToolEventKind.PHASE, {"phase": "authorizing"}))
            decision: ToolOutcome | None = None
            for item in self.middleware:
                decision = await item.authorize(tool, normalized, policy, context)
                if decision is not None:
                    break
            timings["authorization"] = round((time.monotonic() - phase) * 1000)
            if decision is not None:
                execution: ToolExecution = decision
            else:
                phase = time.monotonic()
                await reporter(ToolEvent(ToolEventKind.PHASE, {"phase": "running"}))
                import asyncio

                async def execute_tool() -> ToolExecution:
                    invocation = tool.execute(context, normalized, reporter)
                    if policy.timeout_seconds is None:
                        return await invocation
                    async with asyncio.timeout(policy.timeout_seconds):
                        return await invocation

                execution = asyncio.create_task(execute_tool())
                try:
                    if policy.interrupt_behavior is InterruptBehavior.CANCEL:
                        execution_result = await execution
                    else:
                        execution_result = await asyncio.shield(execution)
                except asyncio.CancelledError:
                    if policy.interrupt_behavior is InterruptBehavior.CANCEL:
                        execution.cancel()
                        await asyncio.gather(execution, return_exceptions=True)
                        raise
                    context.runtime_state["interrupt_pending"] = True
                    while not execution.done():
                        try:
                            await asyncio.shield(execution)
                        except asyncio.CancelledError:
                            continue
                    execution_result = execution.result()
                timings["execution"] = round((time.monotonic() - phase) * 1000)

                execution = execution_result

            if isinstance(execution, ToolPause):
                duration = round((time.monotonic() - started) * 1000)
                timings["total"] = duration
                context.event("tool_paused", name=name, kind=execution.kind)
                return ToolInvocationResult(execution, normalized, policy, timings)

            outcome = execution
            if tool.contract.output_schema is not None and outcome.ok:
                try:
                    compile_json_schema(tool.contract.output_schema).validate(
                        outcome.data
                    )
                except SchemaValidationError as exc:
                    outcome = replace(
                        outcome,
                        status=ToolOutcomeStatus.FAILED,
                        error=str(exc),
                        error_code="invalid_tool_output",
                    )
            for item in reversed(self.middleware):
                outcome = await item.after(tool, normalized, policy, outcome, context)
        except TimeoutError:
            outcome = ToolOutcome.failure(
                "tool execution timed out", code="tool_timeout", executed=False
            )
        except SchemaValidationError as exc:
            outcome = ToolOutcome.failure(str(exc), code=exc.code)
        except Exception as exc:
            outcome = ToolOutcome.failure(
                str(exc) or type(exc).__name__,
                code=getattr(exc, "code", type(exc).__name__),
            )
        duration = round((time.monotonic() - started) * 1000)
        timings["total"] = duration
        context.event("tool_finished", name=name, ok=outcome.ok, duration_ms=duration)
        return ToolInvocationResult(outcome, normalized, policy, timings)

    async def resume(
        self,
        name: str,
        context: ExecutionContext,
        arguments: dict[str, Any],
        pause: ToolPause,
        response: object,
        reporter: ToolReporter = null_reporter,
    ) -> ToolInvocationResult:
        tool = self.catalog._tools.get(name)
        if tool is None:
            return ToolInvocationResult(
                ToolOutcome.failure(
                    f"unsupported tool: {name}", code="unsupported_tool"
                ),
                arguments,
                ResolvedToolPolicy(),
                {},
            )
        if tool.resume is None:
            return ToolInvocationResult(
                ToolOutcome.failure(
                    f"tool does not support resume: {name}",
                    code="tool_resume_unsupported",
                ),
                arguments,
                await self.resolve(name, context, arguments),
                {},
            )
        started = time.monotonic()
        policy = await self.resolve(name, context, arguments)
        execution = await tool.resume(context, arguments, pause, response, reporter)
        return ToolInvocationResult(
            execution,
            arguments,
            policy,
            {"resume": round((time.monotonic() - started) * 1000)},
        )


class ToolRuntime:
    """Small aggregate used by AgentSession and ToolLoop."""

    def __init__(
        self,
        tools: Iterable[ToolDefinition],
        *,
        schema_budget_tokens: int = 8_000,
        middleware: Iterable[ToolMiddleware] = (),
    ) -> None:
        self.catalog = ToolCatalog(tools, schema_budget_tokens=schema_budget_tokens)
        self.executor = ToolExecutor(self.catalog, middleware)

    @classmethod
    def from_catalog(
        cls, catalog: ToolCatalog, middleware: Iterable[ToolMiddleware] = ()
    ) -> "ToolRuntime":
        runtime = cls.__new__(cls)
        runtime.catalog = catalog
        runtime.executor = ToolExecutor(catalog, middleware)
        return runtime

    @property
    def middleware(self) -> tuple[ToolMiddleware, ...]:
        return self.executor.middleware

    @property
    def names(self) -> set[str]:
        return self.catalog.names

    @property
    def schemas(self) -> list[dict[str, object]]:
        return self.catalog.schemas

    def get(self, name: str) -> ToolDefinition | None:
        return self.catalog.get(name)

    def contract(self, name: str):
        return self.catalog.contract(name)

    def snapshot(self) -> ToolCatalogSnapshot:
        return self.catalog.snapshot()

    def discover(self, names: Iterable[str]) -> tuple[str, ...]:
        return self.catalog.discover(names)

    def search(self, query: str, limit: int = 5) -> tuple[str, ...]:
        return self.catalog.search(query, limit)

    def configure_dynamic(
        self, provider, initial: Iterable[ToolDefinition] = ()
    ) -> None:
        self.catalog.configure_dynamic(provider, initial)

    async def refresh_dynamic(self) -> ToolCatalogSnapshot:
        return await self.catalog.refresh_dynamic()

    def combined(self, tools: Iterable[ToolDefinition]) -> "ToolRuntime":
        return ToolRuntime(
            [*self.catalog._tools.values(), *tools],
            schema_budget_tokens=self.catalog.schema_budget_tokens,
            middleware=self.middleware,
        )

    def filtered(self, names: set[str]) -> "ToolRuntime":
        return ToolRuntime(
            [tool for name, tool in self.catalog._tools.items() if name in names],
            schema_budget_tokens=self.catalog.schema_budget_tokens,
            middleware=self.middleware,
        )

    async def resolve(
        self, name: str, context: ExecutionContext, arguments: dict[str, Any]
    ) -> ResolvedToolPolicy:
        return await self.executor.resolve(name, context, arguments)

    async def invoke(
        self,
        name: str,
        context: ExecutionContext,
        arguments: dict[str, Any],
        reporter: ToolReporter = null_reporter,
    ) -> ToolInvocationResult:
        return await self.executor.invoke(name, context, arguments, reporter)

    async def resume(
        self,
        name: str,
        context: ExecutionContext,
        arguments: dict[str, Any],
        pause: ToolPause,
        response: object,
        reporter: ToolReporter = null_reporter,
    ) -> ToolInvocationResult:
        return await self.executor.resume(
            name, context, arguments, pause, response, reporter
        )


__all__ = ["ToolExecutor", "ToolRuntime"]
