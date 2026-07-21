"""Configuration with environment variables taking precedence over TOML."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .credentials import parse_reference, resolve_credential
from .domain import LoopDetectionSettings
from .layout import ProjectLayout


DEFAULT_MAX_TOOL_ROUNDS = 32
DEFAULT_MAX_TURNS = DEFAULT_MAX_TOOL_ROUNDS
CONFIG_VERSION = 2


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
    credential_ref: str | None = None


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
    max_tool_rounds: int
    max_context_messages: int

    @property
    def max_turns(self) -> int:
        """Compatibility alias retained through 2.0.0."""
        return self.max_tool_rounds


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
    tavily_credential_ref: str | None = None


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
    loop_detection: LoopDetectionSettings = LoopDetectionSettings()

    @classmethod
    def load(
        cls, workspace: Path, *, layout: ProjectLayout | None = None
    ) -> "Settings":
        layout = layout or ProjectLayout.discover(workspace)
        document: dict[str, object] = {}
        config = layout.config
        if config.is_file():
            document = load_config_document(config, migrate=True)

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
                max_tool_rounds=_max_tool_rounds(group("runtime")),
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
                tavily_api_key=_web_credential(
                    group("web"),
                    value(
                        "web",
                        "CAPSLOCK_TAVILY_API_KEY",
                        None,
                        "TAVILY_API_KEY",
                    ),
                ),
                web_timeout_seconds=float(
                    value("web", "CAPSLOCK_WEB_TIMEOUT_SECONDS", 20)
                ),
                web_max_bytes=int(value("web", "CAPSLOCK_WEB_MAX_BYTES", 500_000)),
                web_max_redirects=int(value("web", "CAPSLOCK_WEB_MAX_REDIRECTS", 3)),
                tavily_credential_ref=_optional_string(
                    group("web").get("tavily_credential")
                ),
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
            loop_detection=_loop_detection(group("loop_detection")),
        )


@dataclass(frozen=True)
class ConfigIssue:
    severity: str
    code: str
    path: str
    message: str


_GROUP_FIELDS = {
    "model": {
        "api_key",
        "base_url",
        "model",
        "timeout_seconds",
        "input_cost_per_million",
        "output_cost_per_million",
    },
    "runtime": {
        "max_tool_rounds",
        "max_turns",
        "max_context_messages",
        "permission_mode",
    },
    "command": {"command_timeout_seconds", "command_output_bytes"},
    "web": {
        "tavily_api_key",
        "tavily_credential",
        "web_timeout_seconds",
        "web_max_bytes",
        "web_max_redirects",
    },
    "mcp": {"mcp_timeout_seconds", "mcp_output_bytes"},
    "memory": {"enabled"},
    "routing": {"reasoning", "fast", "embedding", "vision"},
    "budget": {"max_run_tokens", "max_run_usd", "max_session_usd"},
    "loop_detection": {
        "consecutive_repeats",
        "failed_retries",
        "cycle_repetitions",
        "max_cycle_length",
    },
}
_TOP_LEVEL = {"config_version", "providers", "models", *_GROUP_FIELDS}
_PROVIDER_FIELDS = {
    "kind",
    "base_url",
    "api_key_env",
    "credential",
    "timeout_seconds",
    "data_policy",
}
_MODEL_FIELDS = {
    "provider",
    "model",
    "context_window",
    "max_output_tokens",
    "input_cost_per_million",
    "output_cost_per_million",
}


def read_config_document(path: Path) -> dict[str, object]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid project configuration: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("project configuration must be a TOML document")
    return document


def validate_config_document(document: dict[str, object]) -> tuple[ConfigIssue, ...]:
    issues: list[ConfigIssue] = []
    version = document.get("config_version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        issues.append(
            ConfigIssue(
                "error", "config_version_type", "config_version", "must be an integer"
            )
        )
    elif version not in {0, 1, CONFIG_VERSION}:
        issues.append(
            ConfigIssue(
                "error",
                "config_version_unsupported",
                "config_version",
                f"unsupported configuration version {version}",
            )
        )
    if version == 0:
        issues.append(
            ConfigIssue(
                "warning",
                "config_deprecated",
                "config_version",
                "v1.8 configuration requires migration",
            )
        )
    if version == 1:
        issues.append(
            ConfigIssue(
                "warning",
                "config_deprecated",
                "config_version",
                "v1.9 configuration requires migration",
            )
        )
    for name in sorted(set(document) - _TOP_LEVEL):
        issues.append(
            ConfigIssue("error", "unknown_field", name, "unknown top-level field")
        )
    for group_name, allowed in _GROUP_FIELDS.items():
        raw = document.get(group_name)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            issues.append(
                ConfigIssue("error", "group_type", group_name, "must be a table")
            )
            continue
        for field in sorted(set(raw) - allowed):
            issues.append(
                ConfigIssue(
                    "error", "unknown_field", f"{group_name}.{field}", "unknown field"
                )
            )
        if group_name == "runtime" and "max_turns" in raw:
            issues.append(
                ConfigIssue(
                    "warning",
                    "max_turns_deprecated",
                    "runtime.max_turns",
                    "use runtime.max_tool_rounds; max_turns is removed in 2.0.0",
                )
            )
        for secret_field in (
            {"api_key"}
            if group_name == "model"
            else {"tavily_api_key"}
            if group_name == "web"
            else set()
        ):
            if raw.get(secret_field):
                issues.append(
                    ConfigIssue(
                        "error",
                        "plaintext_credential",
                        f"{group_name}.{secret_field}",
                        "plaintext credentials are not allowed; use an env: or keyring: reference",
                    )
                )
    for group_name, allowed in (
        ("providers", _PROVIDER_FIELDS),
        ("models", _MODEL_FIELDS),
    ):
        raw = document.get(group_name)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            issues.append(
                ConfigIssue("error", "group_type", group_name, "must be a table")
            )
            continue
        for item_name, item in raw.items():
            if not isinstance(item, dict):
                issues.append(
                    ConfigIssue(
                        "error",
                        "entry_type",
                        f"{group_name}.{item_name}",
                        "must be a table",
                    )
                )
                continue
            for field in sorted(set(item) - allowed):
                issues.append(
                    ConfigIssue(
                        "error",
                        "unknown_field",
                        f"{group_name}.{item_name}.{field}",
                        "unknown field",
                    )
                )
            if group_name == "providers" and "api_key_env" in item:
                issues.append(
                    ConfigIssue(
                        "warning",
                        "api_key_env_deprecated",
                        f"providers.{item_name}.api_key_env",
                        'use credential = "env:NAME"',
                    )
                )
            if group_name == "providers" and "credential" in item:
                try:
                    parse_reference(str(item["credential"]))
                except ValueError as exc:
                    issues.append(
                        ConfigIssue(
                            "error",
                            "credential_reference",
                            f"providers.{item_name}.credential",
                            str(exc),
                        )
                    )
    if not any(item.severity == "error" for item in issues):
        try:
            _validate_semantics(document)
        except (TypeError, ValueError) as exc:
            issues.append(ConfigIssue("error", "config_semantics", "config", str(exc)))
    return tuple(issues)


def load_config_document(path: Path, *, migrate: bool = False) -> dict[str, object]:
    document = read_config_document(path)
    if document.get("config_version", 0) < CONFIG_VERSION and migrate:
        migrate_config(path, apply=True)
        document = read_config_document(path)
    issues = validate_config_document(document)
    errors = [item for item in issues if item.severity == "error"]
    if errors:
        first = errors[0]
        raise ValueError(f"invalid config at {first.path}: {first.message}")
    return document


def migrate_config(path: Path, *, apply: bool) -> tuple[bool, str | None]:
    document = read_config_document(path)
    version = document.get("config_version", 0)
    if version == CONFIG_VERSION:
        return False, None
    if version not in {0, 1}:
        raise ValueError(f"unsupported configuration version {version}")
    plaintext = [
        item
        for item in validate_config_document(document)
        if item.code == "plaintext_credential"
    ]
    if plaintext:
        raise ValueError(plaintext[0].message)
    try:
        import tomlkit
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError("tomlkit is required for configuration migration") from exc
    parsed = tomlkit.parse(path.read_text(encoding="utf-8"))
    parsed["config_version"] = CONFIG_VERSION
    runtime = parsed.get("runtime")
    if runtime is not None and "max_turns" in runtime:
        if "max_tool_rounds" not in runtime:
            runtime["max_tool_rounds"] = runtime["max_turns"]
        del runtime["max_turns"]
    providers = parsed.get("providers")
    if providers is not None:
        for provider in providers.values():
            if "credential" not in provider and "api_key_env" in provider:
                provider["credential"] = f"env:{provider['api_key_env']}"
                del provider["api_key_env"]
    rendered = tomlkit.dumps(parsed)
    if not apply:
        return True, rendered
    backup_dir = path.parent / "state" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"config.v{version}.{stamp}.toml"
    shutil.copy2(path, backup)
    _atomic_write(path, rendered)
    return True, str(backup)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".config-", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def _optional_string(value: object) -> str | None:
    return None if value in {None, ""} else str(value)


def _web_credential(raw: dict[str, object], fallback: object) -> str | None:
    reference = _optional_string(raw.get("tavily_credential"))
    return resolve_credential(reference) if reference else _optional_string(fallback)


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
        credential_ref = str(
            value.get(
                "credential", f"env:{value.get('api_key_env', 'CAPSLOCK_API_KEY')}"
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


def _max_tool_rounds(runtime: dict[str, object]) -> int:
    for name in ("CAPSLOCK_MAX_TOOL_ROUNDS", "CAPSLOCK_MAX_TURNS"):
        if name in os.environ:
            if name == "CAPSLOCK_MAX_TURNS":
                warnings.warn(
                    "CAPSLOCK_MAX_TURNS is deprecated; use CAPSLOCK_MAX_TOOL_ROUNDS",
                    UserWarning,
                    stacklevel=2,
                )
            value = int(os.environ[name])
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            return value
    value = runtime.get(
        "max_tool_rounds", runtime.get("max_turns", DEFAULT_MAX_TOOL_ROUNDS)
    )
    if "max_tool_rounds" not in runtime and "max_turns" in runtime:
        warnings.warn(
            "runtime.max_turns is deprecated; use runtime.max_tool_rounds",
            UserWarning,
            stacklevel=2,
        )
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("runtime.max_tool_rounds must be positive")
    return parsed


def _loop_detection(raw: dict[str, object]) -> LoopDetectionSettings:
    return LoopDetectionSettings(
        consecutive_repeats=int(raw.get("consecutive_repeats", 3)),
        failed_retries=int(raw.get("failed_retries", 3)),
        cycle_repetitions=int(raw.get("cycle_repetitions", 3)),
        max_cycle_length=int(raw.get("max_cycle_length", 4)),
    )


def _validate_semantics(document: dict[str, object]) -> None:
    model = document.get("model", {})
    model = model if isinstance(model, dict) else {}
    legacy = ModelSettings(
        None,
        str(model.get("base_url", "https://api.deepseek.com")),
        str(model.get("model", "deepseek-v4-flash")),
        float(model.get("timeout_seconds", 60)),
        float(model.get("input_cost_per_million", 0)),
        float(model.get("output_cost_per_million", 0)),
    )
    _model_routes(document, legacy, resolve_credentials=False)
    _budget(
        document.get("budget", {}) if isinstance(document.get("budget"), dict) else {}
    )
    runtime = document.get("runtime", {})
    if isinstance(runtime, dict):
        rounds = runtime.get(
            "max_tool_rounds", runtime.get("max_turns", DEFAULT_MAX_TOOL_ROUNDS)
        )
        if int(rounds) <= 0:
            raise ValueError("runtime.max_tool_rounds must be positive")
        if int(runtime.get("max_context_messages", 24)) <= 0:
            raise ValueError("runtime.max_context_messages must be positive")
        if str(runtime.get("permission_mode", "approve_for_me")) not in {
            "full_access",
            "approve_for_me",
            "ask_for_approval",
            "full",
            "approve",
            "ask",
        }:
            raise ValueError("runtime.permission_mode is invalid")
    memory = document.get("memory", {})
    if isinstance(memory, dict):
        _boolean(memory.get("enabled", True))
    loop_detection = document.get("loop_detection", {})
    if isinstance(loop_detection, dict):
        _loop_detection(loop_detection)


def _identifier(kind: str, value: object) -> None:
    if not isinstance(value, str) or not _CONFIG_ID.fullmatch(value):
        raise ValueError(f"invalid {kind} id: {value}")
