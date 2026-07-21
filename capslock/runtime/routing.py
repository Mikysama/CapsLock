"""Deterministic provider routing with metering, retries, and budget gates."""

from __future__ import annotations

import asyncio
import json
import math
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from ..config import (
    BudgetSettings,
    ModelProfileSettings,
    ProviderSettings,
    RoutingSettings,
)
from ..domain import (
    BudgetRequest,
    ModelBudgetExceeded,
    ModelDataPolicyMismatch,
    ModelErrorCode,
    ModelRole,
    ModelRoutingError,
    RunLimits,
)
from ..ports import ModelAuditPort
from .model import (
    ChatModel,
    ModelDelta,
    ModelResponse,
    ModelRunContext,
    ModelRunSession,
    ModelUsage,
    StreamingChatModel,
)


BudgetAuthorizer = Callable[[BudgetRequest], Awaitable[bool]]


@dataclass
class RoutePlan:
    run_id: str
    role: ModelRole
    candidates: list[ModelProfileSettings]
    exclusions: list[dict[str, str]]
    baseline_policy: str


class RouteAttemptDriver:
    """Shared candidate selection and exhausted-route handling."""

    def __init__(self, router: "ModelRouter") -> None:
        self.router = router

    async def plan(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> RoutePlan:
        run_id = self.router._required_run()
        role = self.router._role.get()
        candidates, exclusions = self.router._candidates(role, messages, tools)
        if not candidates:
            await self.router._record_failed_route(run_id, role, exclusions)
            self.router._raise_no_route(exclusions)
        baseline = self.router.profiles[getattr(self.router.routing, role.value)[0]]
        policy = self.router.providers[baseline.provider].data_policy
        return RoutePlan(run_id, role, candidates, exclusions, policy)

    async def select(
        self,
        plan: RoutePlan,
        profile: ModelProfileSettings,
        previous: str | None,
    ) -> tuple[int, ChatModel] | None:
        provider = self.router.providers[profile.provider]
        if provider.data_policy != plan.baseline_policy:
            plan.exclusions.append(
                {"profile": profile.name, "reason": "data_policy_mismatch"}
            )
            return None
        decision_id = await self.router.audit.record_decision(
            plan.run_id,
            role=plan.role.value,
            candidates=self.router._candidate_trace(plan.candidates, plan.exclusions),
            selected=profile.name,
            reasons={"ordered": True, "fallback_from": previous},
        )
        client = self.router.clients.get(profile.provider)
        return None if client is None else (decision_id, client)

    async def exhausted(
        self, plan: RoutePlan, previous: str | None, last_error: Exception | None
    ) -> None:
        if any(
            item.get("reason") == "data_policy_mismatch" for item in plan.exclusions
        ):
            await self.router.audit.record_decision(
                plan.run_id,
                role=plan.role.value,
                candidates=self.router._candidate_trace(
                    plan.candidates, plan.exclusions
                ),
                selected=None,
                reasons={"error": "data_policy_mismatch", "fallback_from": previous},
            )
            raise ModelDataPolicyMismatch(
                "configured fallback would change the provider data policy"
            )
        raise ModelRoutingError(
            str(last_error or "all configured models are unavailable")
        )


class ModelRouter:
    """A ChatModel that selects configured provider/model profiles per call."""

    def __init__(
        self,
        *,
        providers: dict[str, ProviderSettings],
        profiles: dict[str, ModelProfileSettings],
        routing: RoutingSettings,
        clients: dict[str, ChatModel],
        audit: ModelAuditPort | None = None,
        repositories: Any = None,
        budget: BudgetSettings = BudgetSettings(),
        retries: int = 2,
    ) -> None:
        self.providers = providers
        self.profiles = profiles
        self.routing = routing
        self.clients = clients
        if audit is None:
            if repositories is None:
                raise TypeError("ModelRouter requires audit=")
            warnings.warn(
                "ModelRouter(repositories=...) is deprecated; pass audit=; "
                "removed in 2.0.0",
                DeprecationWarning,
                stacklevel=2,
            )
            audit = repositories.models
        self.audit = audit
        self.repositories = repositories
        self.budget = budget
        self.retries = max(0, retries)
        self.attempts = RouteAttemptDriver(self)
        self._run_id: ContextVar[str | None] = ContextVar("model_run_id", default=None)
        self._role: ContextVar[ModelRole] = ContextVar(
            "model_role", default=ModelRole.REASONING
        )
        self._budget_authorizer: BudgetAuthorizer | None = None
        self._run_limits: ContextVar[RunLimits | None] = ContextVar(
            "model_run_limits", default=None
        )
        self._run_budget_base: ContextVar[tuple[int, float]] = ContextVar(
            "model_run_budget_base", default=(0, 0.0)
        )
        self._hard_budget: ContextVar[bool] = ContextVar(
            "model_hard_budget", default=False
        )

    def set_budget_authorizer(self, authorizer: BudgetAuthorizer | None) -> None:
        self._budget_authorizer = authorizer

    @contextmanager
    def _bind_context(self, context: ModelRunContext):
        token = self._run_id.set(context.run_id)
        role_token = self._role.set(context.role)
        limit_token = self._run_limits.set(context.limits)
        base_token = self._run_budget_base.set(context.budget_base)
        hard_token = self._hard_budget.set(context.hard_budget)
        try:
            yield
        finally:
            self._hard_budget.reset(hard_token)
            self._run_budget_base.reset(base_token)
            self._run_limits.reset(limit_token)
            self._role.reset(role_token)
            self._run_id.reset(token)

    def open_session(self, context: ModelRunContext) -> ModelRunSession:
        return _RouterModelRunSession(self, context)

    @contextmanager
    def bind_run(
        self,
        run_id: str,
        *,
        limits: RunLimits | None = None,
        budget_base: tuple[int, float] = (0, 0.0),
        hard: bool = False,
    ):
        warnings.warn(
            "ModelRouter.bind_run() is deprecated; use open_session(); "
            "removed in 2.0.0",
            DeprecationWarning,
            stacklevel=2,
        )
        with self._bind_context(
            ModelRunContext(
                run_id, limits=limits, budget_base=budget_base, hard_budget=hard
            )
        ):
            yield

    @contextmanager
    def use_role(self, role: ModelRole):
        warnings.warn(
            "ModelRouter.use_role() is deprecated; use ModelRunSession.for_role(); "
            "removed in 2.0.0",
            DeprecationWarning,
            stacklevel=2,
        )
        token = self._role.set(role)
        try:
            yield
        finally:
            self._role.reset(token)

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelResponse:
        del model
        plan = await self.attempts.plan(messages, tools)
        run_id, role = plan.run_id, plan.role
        previous: str | None = None
        last_error: Exception | None = None
        for profile in plan.candidates:
            selection = await self.attempts.select(plan, profile, previous)
            if selection is None:
                last_error = ModelRoutingError(
                    f"provider client is unavailable: {profile.provider}"
                )
                previous = profile.name
                continue
            decision_id, client = selection
            for attempt in range(1, self.retries + 2):
                await self._budget_gate(run_id, profile, messages, tools)
                call_id, started = await self._start_call(
                    run_id, decision_id, role, profile, attempt, previous
                )
                try:
                    response = await client.complete(
                        model=profile.model, messages=messages, tools=tools
                    )
                except Exception as exc:
                    last_error = exc
                    code, retryable = _classify_error(exc)
                    await self.audit.finish_call(
                        call_id,
                        duration_ms=_elapsed(started),
                        error_code=code.value,
                        error_message=str(exc) or type(exc).__name__,
                    )
                    if retryable and attempt <= self.retries:
                        await asyncio.sleep(_retry_delay(exc, attempt))
                        continue
                    if not retryable:
                        error = ModelRoutingError(
                            f"non-retryable provider error: {str(exc) or type(exc).__name__}"
                        )
                        error.code = code
                        raise error from exc
                    break
                await self._finish_success(call_id, started, profile, response.usage)
                return response
            previous = profile.name
        await self.attempts.exhausted(plan, previous, last_error)
        raise AssertionError("unreachable")

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ModelDelta]:
        del model
        plan = await self.attempts.plan(messages, tools)
        run_id, role = plan.run_id, plan.role
        previous: str | None = None
        last_error: Exception | None = None
        for profile in plan.candidates:
            selection = await self.attempts.select(plan, profile, previous)
            if selection is None:
                client = None
                decision_id = 0
            else:
                decision_id, client = selection
            if not isinstance(client, StreamingChatModel):
                last_error = ModelRoutingError(
                    f"provider does not support streaming: {profile.provider}"
                )
                previous = profile.name
                continue
            for attempt in range(1, self.retries + 2):
                await self._budget_gate(run_id, profile, messages, tools)
                call_id, started = await self._start_call(
                    run_id, decision_id, role, profile, attempt, previous
                )
                emitted, usage = False, ModelUsage()
                try:
                    async for delta in client.stream_complete(
                        model=profile.model, messages=messages, tools=tools
                    ):
                        emitted = emitted or bool(
                            delta.content
                            or delta.reasoning
                            or delta.tool_index is not None
                        )
                        if delta.usage is not None:
                            usage = delta.usage
                        yield delta
                except Exception as exc:
                    last_error = exc
                    code, retryable = _classify_error(exc)
                    await self.audit.finish_call(
                        call_id,
                        duration_ms=_elapsed(started),
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cost_usd=_cost(profile, usage),
                        error_code=code.value,
                        error_message=str(exc) or type(exc).__name__,
                    )
                    if emitted:
                        raise ModelRoutingError(
                            "model stream failed after output started; retry suppressed"
                        ) from exc
                    if retryable and attempt <= self.retries:
                        await asyncio.sleep(_retry_delay(exc, attempt))
                        continue
                    if not retryable:
                        error = ModelRoutingError(
                            f"non-retryable provider error: {str(exc) or type(exc).__name__}"
                        )
                        error.code = code
                        raise error from exc
                    break
                await self._finish_success(call_id, started, profile, usage)
                return
            previous = profile.name
        await self.attempts.exhausted(plan, previous, last_error)

    async def summary(self, run_id: str) -> list[dict[str, Any]]:
        warnings.warn(
            "ModelRouter.summary(run_id) is deprecated; use ModelRunSession.summary(); "
            "removed in 2.0.0",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.audit.summary(run_id)

    def _required_run(self) -> str:
        run_id = self._run_id.get()
        if not run_id:
            raise ModelRoutingError("model router call is not bound to a run")
        return run_id

    def _candidates(
        self,
        role: ModelRole,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> tuple[list[ModelProfileSettings], list[dict[str, str]]]:
        names = getattr(self.routing, role.value)
        estimated = _estimate_tokens((messages, tools))
        candidates, exclusions = [], []
        override = self._run_limits.get()
        finite_usd_budget = bool(
            self.budget.max_run_usd
            or self.budget.max_session_usd
            or (override and override.max_budget_usd)
        )
        for name in names:
            profile = self.profiles[name]
            provider = self.providers[profile.provider]
            reason = None
            if estimated + profile.max_output_tokens > profile.context_window:
                reason = "context_window"
            elif not provider.api_key:
                reason = "credential_missing"
            elif finite_usd_budget and not (
                profile.input_cost_per_million or profile.output_cost_per_million
            ):
                reason = "price_required"
            if reason:
                exclusions.append({"profile": name, "reason": reason})
            else:
                candidates.append(profile)
        return candidates, exclusions

    async def _budget_gate(
        self,
        run_id: str,
        profile: ModelProfileSettings,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> None:
        input_used, output_used, run_cost = await self.audit.usage(run_id)
        current_tokens = input_used + output_used
        base_tokens, base_cost = self._run_budget_base.get()
        current_tokens += base_tokens
        run_cost += base_cost
        estimated_input = _estimate_tokens((messages, tools))
        reserved_tokens = estimated_input + profile.max_output_tokens
        reserved_cost = (
            estimated_input * profile.input_cost_per_million
            + profile.max_output_tokens * profile.output_cost_per_million
        ) / 1_000_000
        checks = []
        override = self._run_limits.get()
        token_limit = _tighter(
            self.budget.max_run_tokens,
            override.max_tokens if override else None,
        )
        cost_limit = _tighter(
            self.budget.max_run_usd,
            override.max_budget_usd if override else None,
        )
        if token_limit:
            checks.append(
                (
                    "run",
                    "tokens",
                    current_tokens,
                    reserved_tokens,
                    token_limit,
                )
            )
        if cost_limit:
            checks.append(("run", "cost_usd", run_cost, reserved_cost, cost_limit))
        if self.budget.max_session_usd:
            session_cost = await self.audit.session_cost(run_id)
            checks.append(
                (
                    "session",
                    "cost_usd",
                    session_cost,
                    reserved_cost,
                    self.budget.max_session_usd,
                )
            )
        for scope, limit_type, current, reserved, limit in checks:
            if current + reserved <= limit:
                continue
            request = BudgetRequest(
                run_id,
                scope,
                limit_type,
                float(current),
                float(reserved),
                float(limit),
                profile.name,
            )
            allowed = bool(
                not self._hard_budget.get()
                and self._budget_authorizer
                and await self._budget_authorizer(request)
            )
            await self.audit.record_budget(
                run_id,
                scope=scope,
                limit_type=limit_type,
                current=float(current),
                reserved=float(reserved),
                limit=float(limit),
                decision="allowed" if allowed else "hard_stop",
                profile=profile.name,
            )
            if not allowed:
                raise ModelBudgetExceeded(
                    f"{scope} {limit_type} budget would be exceeded by profile {profile.name}",
                    limit_type=limit_type,
                )

    async def _start_call(
        self,
        run_id: str,
        decision_id: int,
        role: ModelRole,
        profile: ModelProfileSettings,
        attempt: int,
        previous: str | None,
    ) -> tuple[str, float]:
        provider = self.providers[profile.provider]
        call_id = await self.audit.start_call(
            run_id,
            decision_id=decision_id,
            role=role.value,
            profile=profile.name,
            provider=provider.name,
            model=profile.model,
            attempt=attempt,
            data_policy=provider.data_policy,
            fallback_from=previous,
        )
        return call_id, time.monotonic()

    async def _finish_success(
        self,
        call_id: str,
        started: float,
        profile: ModelProfileSettings,
        usage: ModelUsage,
    ) -> None:
        if self._usage_required() and not (usage.input_tokens or usage.output_tokens):
            await self.audit.finish_call(
                call_id,
                duration_ms=_elapsed(started),
                error_code=ModelErrorCode.UNAVAILABLE.value,
                error_message="provider did not return usage required by the configured budget",
            )
            raise ModelRoutingError(
                "provider did not return usage required by the configured budget"
            )
        await self.audit.finish_call(
            call_id,
            duration_ms=_elapsed(started),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=_cost(profile, usage),
        )

    def _usage_required(self) -> bool:
        override = self._run_limits.get()
        return bool(
            self.budget.max_run_tokens
            or self.budget.max_run_usd
            or self.budget.max_session_usd
            or (override and (override.max_tokens or override.max_budget_usd))
        )

    async def _record_failed_route(
        self, run_id: str, role: ModelRole, exclusions: list[dict[str, str]]
    ) -> None:
        await self.audit.record_decision(
            run_id,
            role=role.value,
            candidates=exclusions,
            selected=None,
            reasons={"error": "no_eligible_model"},
        )

    @staticmethod
    def _candidate_trace(
        candidates: list[ModelProfileSettings], exclusions: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        return [{"profile": item.name, "eligible": True} for item in candidates] + [
            {**item, "eligible": False} for item in exclusions
        ]

    @staticmethod
    def _raise_no_route(exclusions: list[dict[str, str]]) -> None:
        if exclusions and all(
            item.get("reason") == "data_policy_mismatch" for item in exclusions
        ):
            raise ModelDataPolicyMismatch(
                "no model satisfies the configured data policy"
            )
        raise ModelRoutingError(
            "no eligible model profile: " + json.dumps(exclusions, ensure_ascii=False)
        )


class _RouterModelRunSession(ModelRunSession):
    metered = True

    def __init__(self, router: ModelRouter, context: ModelRunContext) -> None:
        self.router = router
        self.model = router
        self.context = context

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelResponse:
        with self.router._bind_context(self.context):
            return await self.router.complete(
                model=model, messages=messages, tools=tools
            )

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ModelDelta]:
        with self.router._bind_context(self.context):
            async for delta in self.router.stream_complete(
                model=model, messages=messages, tools=tools
            ):
                yield delta

    def for_role(self, role: ModelRole) -> ModelRunSession:
        return _RouterModelRunSession(
            self.router,
            ModelRunContext(
                self.context.run_id,
                role,
                self.context.limits,
                self.context.budget_base,
                self.context.hard_budget,
            ),
        )

    async def summary(self) -> list[dict[str, Any]]:
        return await self.router.audit.summary(self.context.run_id)


def _estimate_tokens(value: object) -> int:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return max(1, math.ceil(len(payload.encode("utf-8")) / 4))


def _tighter(configured, requested):
    if configured is None:
        return requested
    if requested is None:
        return configured
    return min(configured, requested)


def _cost(profile: ModelProfileSettings, usage: ModelUsage) -> float:
    return (
        usage.input_tokens * profile.input_cost_per_million
        + usage.output_tokens * profile.output_cost_per_million
    ) / 1_000_000


def _elapsed(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _classify_error(exc: Exception) -> tuple[ModelErrorCode, bool]:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__.casefold()
    if status == 429 or "ratelimit" in name or "rate_limit" in name:
        return ModelErrorCode.RATE_LIMITED, True
    if status in {401, 403} or "authentication" in name:
        return ModelErrorCode.AUTHENTICATION, False
    if isinstance(status, int) and 400 <= status < 500:
        return ModelErrorCode.INVALID_REQUEST, False
    retryable = (
        status is None or status >= 500 or "timeout" in name or "connection" in name
    )
    return ModelErrorCode.UNAVAILABLE, retryable


def _retry_delay(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        requested = float(raw)
    except (TypeError, ValueError):
        requested = 0.25 * (2 ** (attempt - 1))
    return max(0.0, min(requested, 2.0))
