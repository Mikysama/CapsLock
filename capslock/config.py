"""Configuration with environment variables taking precedence over TOML."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .layout import ProjectLayout


DEFAULT_MAX_TURNS = 32


@dataclass(frozen=True)
class ModelSettings:
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: float
    input_cost_per_million: float
    output_cost_per_million: float


@dataclass(frozen=True)
class ProviderSettings:
    name: str
    kind: str
    base_url: str
    api_key_env: str
    api_key: str | None
    timeout_seconds: float
    data_policy: str


@dataclass(frozen=True)
class ModelProfileSettings:
    name: str
    provider: str
    model: str
    context_window: int
    max_output_tokens: int
    input_cost_per_million: float
    output_cost_per_million: float


@dataclass(frozen=True)
class RoutingSettings:
    reasoning: tuple[str, ...]
    fast: tuple[str, ...]
    embedding: tuple[str, ...]
    vision: tuple[str, ...]


@dataclass(frozen=True)
class BudgetSettings:
    max_run_tokens: int | None = None
    max_run_usd: float | None = None
    max_session_usd: float | None = None


@dataclass(frozen=True)
class RuntimeSettings:
    max_turns: int
    max_context_messages: int


@dataclass(frozen=True)
class CommandSettings:
    command_timeout_seconds: float
    command_output_bytes: int


@dataclass(frozen=True)
class WebSettings:
    tavily_api_key: str | None
    web_timeout_seconds: float
    web_max_bytes: int
    web_max_redirects: int


@dataclass(frozen=True)
class McpSettings:
    mcp_timeout_seconds: float
    mcp_output_bytes: int


@dataclass(frozen=True)
class MemorySettings:
    project_write_enabled: bool = True
    database: Path | None = None


@dataclass(frozen=True)
class Settings:
    model_config: ModelSettings
    runtime: RuntimeSettings
    command: CommandSettings
    web: WebSettings
    mcp: McpSettings
    permission_mode: str
    memory: MemorySettings = MemorySettings()
    providers: dict[str, ProviderSettings] | None = None
    models: dict[str, ModelProfileSettings] | None = None
    routing: RoutingSettings | None = None
    budget: BudgetSettings = BudgetSettings()

    @classmethod
    def load(
        cls, workspace: Path, *, layout: ProjectLayout | None = None
    ) -> "Settings":
        layout = layout or ProjectLayout.discover(workspace)
        document: dict[str, object] = {}
        config = layout.config
        if config.is_file():
            document = tomllib.loads(config.read_text(encoding="utf-8"))

        def group(name: str) -> dict[str, object]:
            values = document.get(name, {})
            return values if isinstance(values, dict) else {}

        def value(group_name: str, name: str, default: object, *aliases: str) -> object:
            for environment_name in (name, *aliases):
                if environment_name in os.environ:
                    return os.environ[environment_name]
            config_name = name.lower().removeprefix("capslock_")
            return group(group_name).get(config_name, default)

        legacy_model = ModelSettings(
            api_key=value("model", "CAPSLOCK_API_KEY", None, "DEEPSEEK_API_KEY"),
            base_url=str(
                value(
                    "model",
                    "CAPSLOCK_BASE_URL",
                    "https://api.deepseek.com",
                    "DEEPSEEK_BASE_URL",
                )
            ),
            model=str(
                value(
                    "model",
                    "CAPSLOCK_MODEL",
                    "deepseek-v4-flash",
                    "DEEPSEEK_MODEL",
                )
            ),
            timeout_seconds=float(value("model", "CAPSLOCK_TIMEOUT_SECONDS", 60)),
            input_cost_per_million=float(
                value("model", "CAPSLOCK_INPUT_COST_PER_MILLION", 0)
            ),
            output_cost_per_million=float(
                value("model", "CAPSLOCK_OUTPUT_COST_PER_MILLION", 0)
            ),
        )
        providers, models, routing = _model_routes(document, legacy_model)
        primary = models[routing.reasoning[0]]
        provider = providers[primary.provider]
        compatibility_model = ModelSettings(
            provider.api_key,
            provider.base_url,
            primary.model,
            provider.timeout_seconds,
            primary.input_cost_per_million,
            primary.output_cost_per_million,
        )
        return cls(
            model_config=compatibility_model,
            runtime=RuntimeSettings(
                max_turns=int(
                    value("runtime", "CAPSLOCK_MAX_TURNS", DEFAULT_MAX_TURNS)
                ),
                max_context_messages=int(
                    value("runtime", "CAPSLOCK_MAX_CONTEXT_MESSAGES", 24)
                ),
            ),
            command=CommandSettings(
                command_timeout_seconds=float(
                    value("command", "CAPSLOCK_COMMAND_TIMEOUT_SECONDS", 120)
                ),
                command_output_bytes=int(
                    value("command", "CAPSLOCK_COMMAND_OUTPUT_BYTES", 100_000)
                ),
            ),
            web=WebSettings(
                tavily_api_key=value(
                    "web",
                    "CAPSLOCK_TAVILY_API_KEY",
                    None,
                    "TAVILY_API_KEY",
                ),
                web_timeout_seconds=float(
                    value("web", "CAPSLOCK_WEB_TIMEOUT_SECONDS", 20)
                ),
                web_max_bytes=int(value("web", "CAPSLOCK_WEB_MAX_BYTES", 500_000)),
                web_max_redirects=int(value("web", "CAPSLOCK_WEB_MAX_REDIRECTS", 3)),
            ),
            mcp=McpSettings(
                mcp_timeout_seconds=float(
                    value("mcp", "CAPSLOCK_MCP_TIMEOUT_SECONDS", 30)
                ),
                mcp_output_bytes=int(
                    value("mcp", "CAPSLOCK_MCP_OUTPUT_BYTES", 100_000)
                ),
            ),
            permission_mode=str(
                value(
                    "runtime",
                    "CAPSLOCK_PERMISSION_MODE",
                    "approve_for_me",
                )
            ),
            memory=MemorySettings(
                project_write_enabled=_boolean(group("memory").get("enabled", True)),
                database=layout.user.memory,
            ),
            providers=providers,
            models=models,
            routing=routing,
            budget=_budget(group("budget")),
        )


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError("memory.enabled must be true or false")


_CONFIG_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


def _model_routes(
    document: dict[str, object], legacy: ModelSettings
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
        return (
            {provider.name: provider},
            {model.name: model},
            RoutingSettings((model.name,), (model.name,), (), ()),
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
            "legacy model environment variables cannot be combined with routed config: "
            + ", ".join(mixed)
        )
    if not isinstance(raw_providers, dict) or not isinstance(raw_models, dict):
        raise ValueError("[providers] and [models] must be configured together")
    providers: dict[str, ProviderSettings] = {}
    for name, value in raw_providers.items():
        _identifier("provider", name)
        if not isinstance(value, dict):
            raise ValueError(f"provider {name} must be a table")
        kind = str(value.get("kind", "openai_compatible"))
        if kind != "openai_compatible":
            raise ValueError(f"unsupported provider kind: {kind}")
        base_url = str(value.get("base_url", "")).rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"provider {name} requires an http(s) base_url")
        key_env = str(value.get("api_key_env", "CAPSLOCK_API_KEY"))
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key_env):
            raise ValueError(f"provider {name} has an invalid api_key_env")
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
            os.environ.get(key_env),
            timeout,
            data_policy,
        )
    models: dict[str, ModelProfileSettings] = {}
    for name, value in raw_models.items():
        _identifier("model profile", name)
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
    routing = RoutingSettings(
        reasoning,
        route("fast", reasoning),
        route("embedding"),
        route("vision"),
    )
    return providers, models, routing


def _budget(raw: dict[str, object]) -> BudgetSettings:
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


def _identifier(kind: str, value: object) -> None:
    if not isinstance(value, str) or not _CONFIG_ID.fullmatch(value):
        raise ValueError(f"invalid {kind} id: {value}")
