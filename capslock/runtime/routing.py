"""Deterministic provider routing with metering, retries, and budget gates."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from ..configuration import (
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
    ProviderCapabilityUnavailable,
    RunLimits,
)
from ..ports import ModelAuditPort
from ..structured_output import StrictSchemaError, prompt_schema_messages
from ..models import SELECTABLE_MODELS
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


class RoutePlanner:
    """Build and advance a deterministic model route plan."""

    def __init__(self, router: "ModelRouter") -> None:
        self.router = router

    async def plan(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> RoutePlan:
        run_id = self.router._required_run()
        role = self.router._role.get()
        candidates, exclusions = self.router._candidates(
            role, messages, tools, max_output_tokens, response_format
        )
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


class ModelBudgetGate:
    """Apply configured run/session reservation limits before each attempt."""

    def __init__(self, router: "ModelRouter") -> None:
        self.router = router

    async def check(
        self,
        run_id: str,
        profile: ModelProfileSettings,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelProfileSettings:
        return await self.router._budget_gate(run_id, profile, messages, tools)


class RouteAttemptExecutor:
    """Own model-call audit lifecycle shared by stream and complete paths."""

    def __init__(self, router: "ModelRouter") -> None:
        self.router = router

    async def start(
        self,
        run_id: str,
        decision_id: int,
        role: ModelRole,
        profile: ModelProfileSettings,
        attempt: int,
        previous: str | None,
    ) -> tuple[int, float]:
        return await self.router._start_call(
            run_id,
            decision_id,
            role,
            profile,
            attempt,
            previous,
        )

    async def finish_success(
        self,
        call_id: int,
        started: float,
        profile: ModelProfileSettings,
        usage: ModelUsage,
    ) -> None:
        await self.router._finish_success(call_id, started, profile, usage)


class ModelRouter:
    """A ChatModel that selects configured provider/model profiles per call."""

    def __init__(
        self,
        *,
        providers: dict[str, ProviderSettings],
        profiles: dict[str, ModelProfileSettings],
        routing: RoutingSettings,
        clients: dict[str, ChatModel],
        audit: ModelAuditPort,
        budget: BudgetSettings = BudgetSettings(),
        retries: int = 2,
    ) -> None:
        self.providers = providers
        self.profiles = profiles
        self.routing = routing
        self.clients = clients
        self.audit = audit
        self.budget = budget
        self.retries = max(0, retries)
        self.planner = RoutePlanner(self)
        self.budget_gate = ModelBudgetGate(self)
        self.attempt_executor = RouteAttemptExecutor(self)
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

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> ModelResponse:
        plan = await self.planner.plan(
            messages, tools, max_output_tokens, response_format
        )
        run_id, role = plan.run_id, plan.role
        previous: str | None = None
        last_error: Exception | None = None
        for configured_profile in plan.candidates:
            profile = _model_override(configured_profile, model, role)
            provider = self.providers[profile.provider]
            request_messages, request_format = _structured_output_request(
                provider, messages, response_format
            )
            effective_profile = replace(
                profile,
                max_output_tokens=min(
                    profile.max_output_tokens,
                    max_output_tokens or profile.max_output_tokens,
                ),
            )
            selection = await self.planner.select(plan, profile, previous)
            if selection is None:
                last_error = ModelRoutingError(
                    f"provider client is unavailable: {profile.provider}"
                )
                previous = profile.name
                continue
            decision_id, client = selection
            for attempt in range(1, self.retries + 2):
                effective_profile = await self.budget_gate.check(
                    run_id, effective_profile, request_messages, tools
                )
                call_id, started = await self.attempt_executor.start(
                    run_id, decision_id, role, profile, attempt, previous
                )
                try:
                    arguments: dict[str, object] = {
                        "model": profile.model,
                        "messages": request_messages,
                        "tools": tools,
                    }
                    arguments["max_output_tokens"] = effective_profile.max_output_tokens
                    if request_format is not None:
                        arguments["response_format"] = request_format
                    response = await client.complete(**arguments)
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
                await self.attempt_executor.finish_success(
                    call_id, started, profile, response.usage
                )
                return response
            previous = profile.name
        await self.planner.exhausted(plan, previous, last_error)
        raise AssertionError("unreachable")

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> AsyncIterator[ModelDelta]:
        plan = await self.planner.plan(
            messages, tools, max_output_tokens, response_format
        )
        run_id, role = plan.run_id, plan.role
        previous: str | None = None
        last_error: Exception | None = None
        for configured_profile in plan.candidates:
            profile = _model_override(configured_profile, model, role)
            provider = self.providers[profile.provider]
            request_messages, request_format = _structured_output_request(
                provider, messages, response_format
            )
            effective_profile = replace(
                profile,
                max_output_tokens=min(
                    profile.max_output_tokens,
                    max_output_tokens or profile.max_output_tokens,
                ),
            )
            selection = await self.planner.select(plan, profile, previous)
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
                effective_profile = await self.budget_gate.check(
                    run_id, effective_profile, request_messages, tools
                )
                call_id, started = await self.attempt_executor.start(
                    run_id, decision_id, role, profile, attempt, previous
                )
                emitted, usage = False, ModelUsage()
                try:
                    arguments: dict[str, object] = {
                        "model": profile.model,
                        "messages": request_messages,
                        "tools": tools,
                    }
                    arguments["max_output_tokens"] = effective_profile.max_output_tokens
                    if request_format is not None:
                        arguments["response_format"] = request_format
                    async for delta in client.stream_complete(**arguments):
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
                await self.attempt_executor.finish_success(
                    call_id, started, profile, usage
                )
                return
            previous = profile.name
        await self.planner.exhausted(plan, previous, last_error)

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
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> tuple[list[ModelProfileSettings], list[dict[str, str]]]:
        names = getattr(self.routing, role.value)
        candidates, prompt_fallbacks, exclusions = [], [], []
        override = self._run_limits.get()
        finite_usd_budget = bool(
            self.budget.max_run_usd
            or self.budget.max_session_usd
            or (override and override.max_budget_usd)
        )
        for name in names:
            configured = self.profiles[name]
            profile = replace(
                configured,
                max_output_tokens=min(
                    configured.max_output_tokens,
                    max_output_tokens or configured.max_output_tokens,
                ),
            )
            provider = self.providers[profile.provider]
            request_messages, request_format = _structured_output_request(
                provider, messages, response_format
            )
            estimated = _estimate_tokens((request_messages, tools, request_format))
            reason = None
            if estimated + profile.max_output_tokens > profile.context_window:
                reason = "context_window"
            elif not provider.api_key:
                reason = "credential_missing"
            elif finite_usd_budget and not (
                profile.input_cost_per_million or profile.output_cost_per_million
            ):
                reason = "price_required"
            elif tools and not provider.strict_tool_calls:
                reason = "strict_tool_calls_unsupported"
            elif (
                response_format is not None
                and response_format.get("type") == "json_schema"
                and not provider.json_schema_outputs
            ):
                prompt_fallbacks.append(profile)
                continue
            if reason:
                exclusions.append({"profile": name, "reason": reason})
            else:
                candidates.append(profile)
        # Prefer provider-enforced structured output regardless of route ordering,
        # then retain the configured order among prompt-fallback providers.
        return candidates + prompt_fallbacks, exclusions

    async def _budget_gate(
        self,
        run_id: str,
        profile: ModelProfileSettings,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelProfileSettings:
        input_used, output_used, run_cost = await self.audit.usage(run_id)
        current_tokens = input_used + output_used
        base_tokens, base_cost = self._run_budget_base.get()
        current_tokens += base_tokens
        run_cost += base_cost
        estimated_input = _estimate_tokens((messages, tools))
        override = self._run_limits.get()
        token_limit = _tighter(
            self.budget.max_run_tokens,
            override.max_tokens if override else None,
        )
        output_limit = profile.max_output_tokens
        if token_limit:
            remaining_output = token_limit - current_tokens - estimated_input
            # A tiny remainder cannot produce a useful model turn and should
            # remain a hard stop. For normal remainders, cap this request to
            # the actual budget instead of reserving the profile default.
            if remaining_output >= min(16, profile.max_output_tokens):
                output_limit = min(output_limit, remaining_output)
        profile = replace(profile, max_output_tokens=max(1, output_limit))
        reserved_tokens = estimated_input + profile.max_output_tokens
        reserved_cost = (
            estimated_input * profile.input_cost_per_million
            + profile.max_output_tokens * profile.output_cost_per_million
        ) / 1_000_000
        checks = []
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
        return profile

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
        if exclusions and any(
            item.get("reason") == "strict_tool_calls_unsupported" for item in exclusions
        ):
            raise ProviderCapabilityUnavailable(
                "no model satisfies the required provider capabilities: "
                + json.dumps(exclusions, ensure_ascii=False)
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
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> ModelResponse:
        with self.router._bind_context(self.context):
            return await self.router.complete(
                model=model,
                messages=messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
                response_format=response_format,
            )

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        max_output_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> AsyncIterator[ModelDelta]:
        with self.router._bind_context(self.context):
            async for delta in self.router.stream_complete(
                model=model,
                messages=messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
                response_format=response_format,
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


def _structured_output_request(
    provider: ProviderSettings,
    messages: list[dict[str, object]],
    response_format: dict[str, object] | None,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    if (
        response_format is not None
        and response_format.get("type") == "json_schema"
        and not provider.json_schema_outputs
    ):
        return prompt_schema_messages(messages, response_format), None
    return messages, response_format


def _model_override(
    profile: ModelProfileSettings, requested: str, role: ModelRole
) -> ModelProfileSettings:
    """Apply only the small interactive model allowlist to an existing route."""

    if (
        role is ModelRole.REASONING
        and requested in SELECTABLE_MODELS
        and requested != profile.model
    ):
        return replace(profile, model=requested)
    return profile


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
    if isinstance(exc, StrictSchemaError):
        return ModelErrorCode.INVALID_REQUEST, False
    if status == 413 or _is_context_overflow(exc):
        return ModelErrorCode.CONTEXT_OVERFLOW, False
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


_CONTEXT_OVERFLOW_CODES = {
    "context_length_exceeded",
    "prompt_too_long",
    "input_too_long",
    "request_too_large",
}
_CONTEXT_OVERFLOW_TEXT = (
    "context length exceeded",
    "context_length_exceeded",
    "prompt is too long",
    "prompt_too_long",
    "input is too long",
    "input_too_long",
    "maximum context length",
    "exceeds the context window",
    "request too large for the model",
)


def _is_context_overflow(exc: Exception) -> bool:
    """Prefer provider codes, falling back only to explicit overflow wording."""

    values: list[object] = [
        getattr(exc, "code", None),
        getattr(exc, "type", None),
    ]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        values.extend(_error_values(body))
    error = getattr(exc, "error", None)
    if isinstance(error, dict):
        values.extend(_error_values(error))
    else:
        values.extend((getattr(error, "code", None), getattr(error, "type", None)))
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            values.extend(_error_values(payload))
    normalized = {str(value).casefold() for value in values if value is not None}
    if normalized & _CONTEXT_OVERFLOW_CODES:
        return True
    text = str(exc).casefold()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_TEXT)


def _error_values(value: dict[str, object]) -> list[object]:
    nested = value.get("error")
    values = [value.get("code"), value.get("type")]
    if isinstance(nested, dict):
        values.extend((nested.get("code"), nested.get("type")))
    return values


def _retry_delay(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        requested = float(raw)
    except (TypeError, ValueError):
        requested = 0.25 * (2 ** (attempt - 1))
    return max(0.0, min(requested, 2.0))
