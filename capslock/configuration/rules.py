"""Configuration parsing rules shared by validation and resolution."""

from __future__ import annotations

import os
import re

from ..credentials import parse_reference, resolve_credential
from ..domain import LoopDetectionSettings
from .types import (
    AgentSettings,
    BudgetSettings,
    ModelProfileSettings,
    ModelSettings,
    ProviderSettings,
    RoutingSettings,
)


DEFAULT_MAX_TOOL_ROUNDS = 32
_CONFIG_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


def optional_string(value: object) -> str | None:
    return None if value in {None, ""} else str(value)


def web_credential(raw: dict[str, object], fallback: object) -> str | None:
    reference = optional_string(raw.get("tavily_credential"))
    return resolve_credential(reference) if reference else optional_string(fallback)


def boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError("memory.enabled must be true or false")


def model_routes(
    document: dict[str, object],
    legacy: ModelSettings,
    *,
    resolve_credentials: bool = True,
) -> tuple[
    dict[str, ProviderSettings],
    dict[str, ModelProfileSettings],
    RoutingSettings,
]:
    raw_providers = document.get("providers")
    raw_models = document.get("models")
    using_routes = any(name in document for name in ("providers", "models", "routing"))
    if not using_routes:
        provider = ProviderSettings(
            "default",
            "openai_compatible",
            legacy.base_url,
            "CAPSLOCK_API_KEY",
            legacy.api_key,
            legacy.timeout_seconds,
            "provider:default",
            "env:CAPSLOCK_API_KEY",
        )
        model = ModelProfileSettings(
            "default",
            provider.name,
            legacy.model,
            128_000,
            8_192,
            legacy.input_cost_per_million,
            legacy.output_cost_per_million,
        )
        route = (model.name,)
        return (
            {provider.name: provider},
            {model.name: model},
            RoutingSettings(route, route, (), ()),
        )
    if isinstance(document.get("model"), dict) and document["model"]:
        raise ValueError("legacy [model] cannot be combined with [providers]/[models]")
    legacy_environment = {
        "CAPSLOCK_BASE_URL",
        "CAPSLOCK_MODEL",
        "CAPSLOCK_TIMEOUT_SECONDS",
        "CAPSLOCK_INPUT_COST_PER_MILLION",
        "CAPSLOCK_OUTPUT_COST_PER_MILLION",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
    }
    mixed = sorted(legacy_environment.intersection(os.environ))
    if mixed:
        raise ValueError(
            "legacy model environment variables cannot be combined with routed "
            "config: " + ", ".join(mixed)
        )
    if not isinstance(raw_providers, dict) or not isinstance(raw_models, dict):
        raise ValueError("[providers] and [models] must be configured together")
    providers: dict[str, ProviderSettings] = {}
    for name, value in raw_providers.items():
        identifier("provider", name)
        if not isinstance(value, dict):
            raise ValueError(f"provider {name} must be a table")
        kind = str(value.get("kind", "openai_compatible"))
        if kind != "openai_compatible":
            raise ValueError(f"unsupported provider kind: {kind}")
        base_url = str(value.get("base_url", "")).rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"provider {name} requires an http(s) base_url")
        credential_ref = str(
            value.get(
                "credential",
                f"env:{value.get('api_key_env', 'CAPSLOCK_API_KEY')}",
            )
        )
        source, credential_name = parse_reference(credential_ref)
        key_env = credential_name if source == "env" else ""
        timeout = float(value.get("timeout_seconds", 60))
        if timeout <= 0:
            raise ValueError(f"provider {name} timeout_seconds must be positive")
        data_policy = str(value.get("data_policy", f"provider:{name}")).strip()
        if not data_policy or len(data_policy) > 128:
            raise ValueError(f"provider {name} has an invalid data_policy")
        providers[name] = ProviderSettings(
            name,
            kind,
            base_url,
            key_env,
            resolve_credential(credential_ref) if resolve_credentials else None,
            timeout,
            data_policy,
            credential_ref,
        )
    models: dict[str, ModelProfileSettings] = {}
    for name, value in raw_models.items():
        identifier("model profile", name)
        if not isinstance(value, dict):
            raise ValueError(f"model profile {name} must be a table")
        provider_name = str(value.get("provider", ""))
        if provider_name not in providers:
            raise ValueError(f"model profile {name} references unknown provider")
        remote_name = str(value.get("model", "")).strip()
        if not remote_name:
            raise ValueError(f"model profile {name} requires model")
        context_window = int(value.get("context_window", 128_000))
        max_output = int(value.get("max_output_tokens", 8_192))
        input_cost = float(value.get("input_cost_per_million", 0))
        output_cost = float(value.get("output_cost_per_million", 0))
        if min(context_window, max_output) <= 0 or min(input_cost, output_cost) < 0:
            raise ValueError(f"model profile {name} has invalid limits or prices")
        models[name] = ModelProfileSettings(
            name,
            provider_name,
            remote_name,
            context_window,
            max_output,
            input_cost,
            output_cost,
        )
    remote_limits: dict[tuple[str, str], int] = {}
    for item in models.values():
        key = (item.provider, item.model)
        previous = remote_limits.setdefault(key, item.max_output_tokens)
        if previous != item.max_output_tokens:
            raise ValueError(
                "profiles sharing a provider/model must use the same max_output_tokens"
            )
    raw_routing = document.get("routing", {})
    if not isinstance(raw_routing, dict):
        raise ValueError("[routing] must be a table")

    def route(role: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        raw = raw_routing.get(role, default)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f"routing.{role} must be an array")
        result = tuple(str(item) for item in raw)
        missing = [item for item in result if item not in models]
        if missing:
            raise ValueError(f"routing.{role} references unknown profiles: {missing}")
        return result

    reasoning = route("reasoning")
    if not reasoning:
        raise ValueError("routing.reasoning requires at least one model profile")
    return (
        providers,
        models,
        RoutingSettings(
            reasoning,
            route("fast", reasoning),
            route("embedding"),
            route("vision"),
        ),
    )


def budget_settings(raw: dict[str, object]) -> BudgetSettings:
    def positive(name: str, cast):
        value = raw.get(name)
        if value in {None, ""}:
            return None
        parsed = cast(value)
        if parsed <= 0:
            raise ValueError(f"budget.{name} must be positive")
        return parsed

    return BudgetSettings(
        positive("max_run_tokens", int),
        positive("max_run_usd", float),
        positive("max_session_usd", float),
    )


def agent_settings(raw: dict[str, object]) -> AgentSettings:
    values = AgentSettings(
        enabled=boolean(raw.get("enabled", True)),
        max_children=int(raw.get("max_children", 4)),
        max_concurrency=int(raw.get("max_concurrency", 2)),
        max_depth=int(raw.get("max_depth", 1)),
        max_child_tool_rounds=int(raw.get("max_child_tool_rounds", 16)),
    )
    if values.max_children < 1 or values.max_children > 32:
        raise ValueError("agents.max_children must be between 1 and 32")
    if values.max_concurrency < 1 or values.max_concurrency > values.max_children:
        raise ValueError("agents.max_concurrency must be between 1 and max_children")
    if values.max_depth != 1:
        raise ValueError("agents.max_depth must be 1")
    if values.max_child_tool_rounds < 1:
        raise ValueError("agents.max_child_tool_rounds must be positive")
    return values


def max_tool_rounds(runtime: dict[str, object]) -> int:
    if "CAPSLOCK_MAX_TURNS" in os.environ:
        raise ValueError(
            "CAPSLOCK_MAX_TURNS was removed in 2.0.0; use CAPSLOCK_MAX_TOOL_ROUNDS"
        )
    if "max_turns" in runtime:
        raise ValueError(
            "runtime.max_turns was removed in 2.0.0; use runtime.max_tool_rounds"
        )
    value = os.environ.get(
        "CAPSLOCK_MAX_TOOL_ROUNDS",
        runtime.get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS),
    )
    parsed = int(value)
    if parsed <= 0:
        source = (
            "CAPSLOCK_MAX_TOOL_ROUNDS"
            if "CAPSLOCK_MAX_TOOL_ROUNDS" in os.environ
            else "runtime.max_tool_rounds"
        )
        raise ValueError(f"{source} must be positive")
    return parsed


def loop_detection_settings(raw: dict[str, object]) -> LoopDetectionSettings:
    return LoopDetectionSettings(
        consecutive_repeats=int(raw.get("consecutive_repeats", 3)),
        failed_retries=int(raw.get("failed_retries", 3)),
        cycle_repetitions=int(raw.get("cycle_repetitions", 3)),
        max_cycle_length=int(raw.get("max_cycle_length", 4)),
    )


def identifier(kind: str, value: object) -> None:
    if not isinstance(value, str) or not _CONFIG_ID.fullmatch(value):
        raise ValueError(f"invalid {kind} id: {value}")
