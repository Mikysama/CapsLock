"""Environment-aware typed settings resolution."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from ..layout import ProjectLayout
from .rules import (
    agent_settings,
    boolean,
    budget_settings,
    loop_detection_settings,
    max_tool_rounds,
    model_routes,
    optional_string,
    web_credential,
)
from .types import (
    CommandSettings,
    McpSettings,
    MemorySettings,
    ModelSettings,
    RuntimeSettings,
    WebSettings,
)


def resolve_settings(
    document: dict[str, object],
    *,
    layout: ProjectLayout,
    settings_factory: Callable[..., Any],
):
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
    providers, models, routing = model_routes(document, legacy_model)
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
    return settings_factory(
        model_config=compatibility_model,
        runtime=RuntimeSettings(
            max_tool_rounds=max_tool_rounds(group("runtime")),
            max_context_messages=int(
                value("runtime", "CAPSLOCK_MAX_CONTEXT_MESSAGES", 24)
            ),
        ),
        agents=agent_settings(group("agents")),
        command=CommandSettings(
            command_timeout_seconds=float(
                value("command", "CAPSLOCK_COMMAND_TIMEOUT_SECONDS", 120)
            ),
            command_output_bytes=int(
                value("command", "CAPSLOCK_COMMAND_OUTPUT_BYTES", 100_000)
            ),
        ),
        web=WebSettings(
            tavily_api_key=web_credential(
                group("web"),
                value(
                    "web",
                    "CAPSLOCK_TAVILY_API_KEY",
                    None,
                    "TAVILY_API_KEY",
                ),
            ),
            web_timeout_seconds=float(value("web", "CAPSLOCK_WEB_TIMEOUT_SECONDS", 20)),
            web_max_bytes=int(value("web", "CAPSLOCK_WEB_MAX_BYTES", 500_000)),
            web_max_redirects=int(value("web", "CAPSLOCK_WEB_MAX_REDIRECTS", 3)),
            tavily_credential_ref=optional_string(
                group("web").get("tavily_credential")
            ),
        ),
        mcp=McpSettings(
            mcp_timeout_seconds=float(value("mcp", "CAPSLOCK_MCP_TIMEOUT_SECONDS", 30)),
            mcp_output_bytes=int(value("mcp", "CAPSLOCK_MCP_OUTPUT_BYTES", 100_000)),
        ),
        permission_mode=str(
            value("runtime", "CAPSLOCK_PERMISSION_MODE", "approve_for_me")
        ),
        memory=MemorySettings(
            project_write_enabled=boolean(group("memory").get("enabled", True)),
            database=layout.user.memory,
        ),
        providers=providers,
        models=models,
        routing=routing,
        budget=budget_settings(group("budget")),
        loop_detection=loop_detection_settings(group("loop_detection")),
    )
