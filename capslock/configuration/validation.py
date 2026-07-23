"""Pure configuration document validation."""

from __future__ import annotations

from ..credentials import parse_reference
from .rules import (
    DEFAULT_MAX_TOOL_ROUNDS,
    agent_settings,
    boolean,
    budget_settings,
    loop_detection_settings,
    model_routes,
)
from .types import ConfigIssue, ModelSettings


CONFIG_VERSION = 2
_GROUP_FIELDS = {
    "model": {
        "api_key",
        "base_url",
        "model",
        "timeout_seconds",
        "input_cost_per_million",
        "output_cost_per_million",
    },
    "runtime": {"max_tool_rounds", "max_context_messages", "permission_mode"},
    "agents": {
        "enabled",
        "max_children",
        "max_concurrency",
        "max_depth",
        "max_child_tool_rounds",
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
    if version in {0, 1}:
        issues.append(
            ConfigIssue(
                "warning",
                "config_deprecated",
                "config_version",
                f"v{'1.8' if version == 0 else '1.9'} configuration requires migration",
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
            if group_name == "runtime" and field == "max_turns":
                issues.append(
                    ConfigIssue(
                        "error",
                        "max_turns_removed",
                        "runtime.max_turns",
                        "removed in 2.0.0; use runtime.max_tool_rounds",
                    )
                )
            else:
                issues.append(
                    ConfigIssue(
                        "error",
                        "unknown_field",
                        f"{group_name}.{field}",
                        "unknown field",
                    )
                )
        secret_fields = (
            {"api_key"}
            if group_name == "model"
            else {"tavily_api_key"}
            if group_name == "web"
            else set()
        )
        for field in secret_fields:
            if raw.get(field):
                issues.append(
                    ConfigIssue(
                        "error",
                        "plaintext_credential",
                        f"{group_name}.{field}",
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
            path = f"{group_name}.{item_name}"
            if not isinstance(item, dict):
                issues.append(
                    ConfigIssue("error", "entry_type", path, "must be a table")
                )
                continue
            for field in sorted(set(item) - allowed):
                issues.append(
                    ConfigIssue(
                        "error", "unknown_field", f"{path}.{field}", "unknown field"
                    )
                )
            if group_name == "providers" and "api_key_env" in item:
                issues.append(
                    ConfigIssue(
                        "warning",
                        "api_key_env_deprecated",
                        f"{path}.api_key_env",
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
                            f"{path}.credential",
                            str(exc),
                        )
                    )
    if not any(item.severity == "error" for item in issues):
        try:
            validate_semantics(document)
        except (TypeError, ValueError) as exc:
            issues.append(ConfigIssue("error", "config_semantics", "config", str(exc)))
    return tuple(issues)


def validate_semantics(document: dict[str, object]) -> None:
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
    model_routes(document, legacy, resolve_credentials=False)
    budget_settings(
        document.get("budget", {}) if isinstance(document.get("budget"), dict) else {}
    )
    agents = document.get("agents", {})
    if isinstance(agents, dict):
        agent_settings(agents)
    runtime = document.get("runtime", {})
    if isinstance(runtime, dict):
        if int(runtime.get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS)) <= 0:
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
        boolean(memory.get("enabled", True))
    loop_detection = document.get("loop_detection", {})
    if isinstance(loop_detection, dict):
        loop_detection_settings(loop_detection)
