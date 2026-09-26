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
    ToolExecutionState,
    ToolExecution,
    ToolInvocationResult,
    ToolMiddleware,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
    ToolReporter,
    ToolSelectionMode,
    null_reporter,
)
from .schema import SchemaValidationError, compile_json_schema, strip_optional_nulls
from ..policy import InvalidPathError, PolicyError


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
        normalized = strip_optional_nulls(arguments, tool.contract.input_schema)
        assert isinstance(normalized, dict)
        compile_json_schema(tool.contract.input_schema).validate(normalized)
        await tool.validate(normalized, context)
        return await tool.resolve_policy(normalized, context)

    async def invoke(
        self,
        name: str,
        context: ExecutionContext,
        arguments: dict[str, Any],
        reporter: ToolReporter = null_reporter,
    ) -> ToolInvocationResult:
        return await self._run(name, context, arguments, reporter)

    async def _run(
        self,
        name: str,
        context: ExecutionContext,
        arguments: dict[str, Any],
        reporter: ToolReporter,
        *,
        pause: ToolPause | None = None,
        response: object = None,
    ) -> ToolInvocationResult:
        tool = self.catalog._tools.get(name)
        if tool is None:
            suggestions = list(self.catalog.candidates(name, 3))
            return ToolInvocationResult(
                ToolOutcome.failure(
                    f"unsupported tool: {name}",
                    code="unsupported_tool",
                    data={
                        "path": "$.name",
                        "expected": "registered tool name",
                        "received_type": "string",
                        "retryable": bool(suggestions),
                        "suggested_tools": suggestions,
                        "repair_attempt": 0,
                    },
                ),
                arguments,
                ResolvedToolPolicy(),
                {},
            )
        if pause is not None and tool.resume is None:
            return ToolInvocationResult(
                ToolOutcome.failure(
                    f"tool does not support resume: {name}",
                    code="tool_resume_unsupported",
                ),
                arguments,
                ResolvedToolPolicy(),
                {},
            )
        normalized = dict(arguments)
        timings: dict[str, int] = {}
        policy = ResolvedToolPolicy()
        execution_started = False
        completed_outcome: ToolOutcome | None = None
        postprocessing_failed = False
        context.event("tool_started", name=name)
        started = time.monotonic()
        try:
            phase = time.monotonic()
            await reporter(ToolEvent(ToolEventKind.PHASE, {"phase": "validating"}))
            stripped = strip_optional_nulls(normalized, tool.contract.input_schema)
            assert isinstance(stripped, dict)
            normalized = stripped
            for item in self.middleware:
                normalized = await item.normalize(tool, normalized, context)
            compile_json_schema(tool.contract.input_schema).validate(normalized)
            await tool.validate(normalized, context)
            early_decision: ToolExecution | None = None
            for item in self.middleware:
                pre_authorize = getattr(item, "pre_authorize", None)
                if callable(pre_authorize):
                    early_decision = await pre_authorize(tool, normalized, context)
                    if early_decision is not None:
                        break
            if early_decision is None:
                policy = await tool.resolve_policy(normalized, context)
            timings["validation"] = round((time.monotonic() - phase) * 1000)

            phase = time.monotonic()
            await reporter(ToolEvent(ToolEventKind.PHASE, {"phase": "authorizing"}))
            decision: ToolExecution | None = early_decision
            if decision is None:
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

                execution_started = True

                async def execute_tool() -> ToolExecution:
                    if pause is None:
                        invocation = tool.execute(context, normalized, reporter)
                    else:
                        assert tool.resume is not None
                        invocation = tool.resume(
                            context, normalized, pause, response, reporter
                        )
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

            completed_outcome = execution
            outcome = self._validate_output(tool, execution)
            for item in reversed(self.middleware):
                outcome = await item.after(tool, normalized, policy, outcome, context)
            outcome = self._validate_output(tool, outcome)
        except TimeoutError:
            postprocessing_failed = completed_outcome is not None
            uncertain = execution_started and (
                policy.external_side_effects
                or policy.destructive
                or policy.context_mutation
            )
            outcome = ToolOutcome.failure(
                "tool execution timed out",
                code="unknown_execution" if uncertain else "tool_timeout",
                executed=False,
                execution_state=(
                    ToolExecutionState.UNKNOWN
                    if uncertain
                    else ToolExecutionState.NOT_STARTED
                ),
                data={
                    "path": "$",
                    "expected": "tool completion before timeout",
                    "received_type": "timeout",
                    "retryable": False,
                    "suggested_tools": [],
                    "repair_attempt": 0,
                },
            )
        except SchemaValidationError as exc:
            postprocessing_failed = completed_outcome is not None
            outcome = ToolOutcome.failure(str(exc), code=exc.code, data=exc.detail())
        except InvalidPathError as exc:
            postprocessing_failed = completed_outcome is not None
            outcome = ToolOutcome.failure(
                str(exc),
                code="invalid_path",
                data={
                    "path": "$.path",
                    "expected": "repository-relative file path",
                    "received_type": "string",
                    "retryable": True,
                    "suggested_tools": [name],
                    "repair_attempt": 0,
                },
            )
        except PolicyError as exc:
            postprocessing_failed = completed_outcome is not None
            message = str(exc)
            retryable = (
                "file does not exist:" in message or "path is a directory:" in message
            )
            outcome = ToolOutcome.failure(
                message,
                code="invalid_path" if retryable else "policy_denied",
                data={
                    "path": "$.path",
                    "expected": "existing repository file"
                    if name in {"read_file", "edit_file"}
                    else "allowed workspace path",
                    "received_type": "string",
                    "retryable": retryable,
                    "suggested_tools": [name] if retryable else [],
                    "repair_attempt": 0,
                },
            )
        except Exception as exc:
            postprocessing_failed = completed_outcome is not None
            uncertain = execution_started and (
                policy.external_side_effects
                or policy.destructive
                or policy.context_mutation
            )
            outcome = ToolOutcome.failure(
                str(exc) or type(exc).__name__,
                code=(
                    "unknown_execution"
                    if uncertain
                    else getattr(exc, "code", type(exc).__name__)
                ),
                execution_state=(
                    ToolExecutionState.UNKNOWN
                    if uncertain
                    else ToolExecutionState.NOT_STARTED
                ),
                data={
                    "path": "$",
                    "expected": "successful tool execution",
                    "received_type": type(exc).__name__,
                    "retryable": False,
                    "suggested_tools": [],
                    "repair_attempt": 0,
                },
            )
        if (
            completed_outcome is None
            and execution_started
            and (
                policy.external_side_effects
                or policy.destructive
                or policy.context_mutation
            )
            and outcome.error_code
            in {"invalid_tool_arguments", "invalid_path", "policy_denied"}
        ):
            outcome = replace(
                outcome,
                error_code="unknown_execution",
                execution_state=ToolExecutionState.UNKNOWN,
                data={
                    **(outcome.data if isinstance(outcome.data, dict) else {}),
                    "retryable": False,
                },
            )
        # Postprocessing errors cannot erase a handler's confirmed side effect
        # or invite parameter repair to execute that side effect a second time.
        if (
            completed_outcome is not None
            and not outcome.ok
            and (postprocessing_failed or completed_outcome.ok)
        ):
            outcome = replace(
                outcome,
                executed=completed_outcome.executed,
                execution_state=completed_outcome.effective_execution_state,
                data={**outcome.data, "retryable": False}
                if isinstance(outcome.data, dict) and "retryable" in outcome.data
                else outcome.data,
                error_code=(
                    "tool_postprocessing_failed"
                    if outcome.error_code
                    in {
                        "invalid_tool_arguments",
                        "invalid_path",
                        "unknown_execution",
                        "tool_timeout",
                    }
                    else outcome.error_code
                ),
            )
        duration = round((time.monotonic() - started) * 1000)
        timings["total"] = duration
        context.event("tool_finished", name=name, ok=outcome.ok, duration_ms=duration)
        return ToolInvocationResult(outcome, normalized, policy, timings)

    @staticmethod
    def _validate_output(tool: ToolDefinition, outcome: ToolOutcome) -> ToolOutcome:
        if tool.contract.output_schema is not None and outcome.ok:
            try:
                compile_json_schema(tool.contract.output_schema).validate(outcome.data)
            except SchemaValidationError as exc:
                return replace(
                    outcome,
                    status=ToolOutcomeStatus.FAILED,
                    error=str(exc),
                    error_code="invalid_tool_output",
                )
        return outcome

    async def resume(
        self,
        name: str,
        context: ExecutionContext,
        arguments: dict[str, Any],
        pause: ToolPause,
        response: object,
        reporter: ToolReporter = null_reporter,
    ) -> ToolInvocationResult:
        return await self._run(
            name,
            context,
            arguments,
            reporter,
            pause=pause,
            response=response,
        )


class ToolRuntime:
    """Small aggregate used by AgentSession and ToolLoop."""

    def __init__(
        self,
        tools: Iterable[ToolDefinition],
        *,
        schema_budget_tokens: int = 8_000,
        middleware: Iterable[ToolMiddleware] = (),
        selection_mode: ToolSelectionMode | str = ToolSelectionMode.SHADOW,
        selection_limit: int = 12,
    ) -> None:
        self.catalog = ToolCatalog(tools, schema_budget_tokens=schema_budget_tokens)
        self.executor = ToolExecutor(self.catalog, middleware)
        self.selection_mode = ToolSelectionMode(selection_mode)
        self.selection_limit = max(3, selection_limit)

    @classmethod
    def from_catalog(
        cls, catalog: ToolCatalog, middleware: Iterable[ToolMiddleware] = ()
    ) -> "ToolRuntime":
        runtime = cls.__new__(cls)
        runtime.catalog = catalog
        runtime.executor = ToolExecutor(catalog, middleware)
        runtime.selection_mode = ToolSelectionMode.SHADOW
        runtime.selection_limit = 12
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

    @property
    def plan_schemas(self) -> list[dict[str, object]]:
        return self.catalog.plan_schemas

    def get(self, name: str) -> ToolDefinition | None:
        return self.catalog.get(name)

    def contract(self, name: str):
        return self.catalog.contract(name)

    def snapshot(self) -> ToolCatalogSnapshot:
        return self.catalog.snapshot()

    def discover(self, names: Iterable[str]) -> tuple[str, ...]:
        return self.catalog.discover(names)

    def search(
        self, query: str, limit: int = 5, *, plan_visible_only: bool = False
    ) -> tuple[str, ...]:
        return self.catalog.search(query, limit, plan_visible_only=plan_visible_only)

    def candidates(self, query: str, limit: int = 3) -> tuple[str, ...]:
        return self.catalog.candidates(query, limit)

    def model_schemas(
        self, query: str, *, planning: bool = False
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        selected = self.catalog.selected_names(
            query,
            limit=self.selection_limit,
            planning=planning,
        )
        all_schemas = self.plan_schemas if planning else self.schemas
        if self.selection_mode is ToolSelectionMode.FILTERED:
            return self.catalog.schemas_for(selected, planning=planning), selected
        return all_schemas, selected

    def configure_dynamic(
        self, provider, initial: Iterable[ToolDefinition] = ()
    ) -> None:
        self.catalog.configure_dynamic(provider, initial)

    async def refresh_dynamic(self) -> ToolCatalogSnapshot:
        return await self.catalog.refresh_dynamic()

    def pop_refresh_diagnostics(self) -> tuple[dict[str, str], ...]:
        return self.catalog.pop_refresh_diagnostics()

    def combined(self, tools: Iterable[ToolDefinition]) -> "ToolRuntime":
        return ToolRuntime(
            [*self.catalog._tools.values(), *tools],
            schema_budget_tokens=self.catalog.schema_budget_tokens,
            middleware=self.middleware,
            selection_mode=self.selection_mode,
            selection_limit=self.selection_limit,
        )

    def filtered(self, names: set[str]) -> "ToolRuntime":
        return ToolRuntime(
            [tool for name, tool in self.catalog._tools.items() if name in names],
            schema_budget_tokens=self.catalog.schema_budget_tokens,
            middleware=self.middleware,
            selection_mode=self.selection_mode,
            selection_limit=self.selection_limit,
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
