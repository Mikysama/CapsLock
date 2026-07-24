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
    ContextSettings,
    DocumentSettings,
    LspServerSettings,
    LspSettings,
    McpSettings,
    MemorySettings,
    ModelProfileSettings,
    ModelSettings,
    ProviderSettings,
    RoutingSettings,
    RuntimeSettings,
    ShellSettings,
    ToolSettings,
    WebSettings,
    WorktreeSettings,
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

    def value(group_name: str, name: str, default: object) -> object:
        if name in os.environ:
            return os.environ[name]
        config_name = name.lower().removeprefix("capslock_")
        return group(group_name).get(config_name, default)

    if document:
        providers, models, routing = model_routes(document)
    else:
        provider = ProviderSettings(
            name="default",
            kind="openai_compatible",
            base_url=os.environ.get("CAPSLOCK_BASE_URL", "https://api.deepseek.com"),
            api_key=os.environ.get("CAPSLOCK_API_KEY"),
            timeout_seconds=float(os.environ.get("CAPSLOCK_TIMEOUT_SECONDS", 60)),
            data_policy="provider:default",
            credential_ref="env:CAPSLOCK_API_KEY",
        )
        profile = ModelProfileSettings(
            name="default",
            provider=provider.name,
            model=os.environ.get("CAPSLOCK_MODEL", "deepseek-v4-flash"),
            context_window=128_000,
            max_output_tokens=8_192,
            input_cost_per_million=float(
                os.environ.get("CAPSLOCK_INPUT_COST_PER_MILLION", 0)
            ),
            output_cost_per_million=float(
                os.environ.get("CAPSLOCK_OUTPUT_COST_PER_MILLION", 0)
            ),
        )
        providers = {provider.name: provider}
        models = {profile.name: profile}
        routing = RoutingSettings((profile.name,), (profile.name,), (), ())
    primary = models[routing.reasoning[0]]
    provider = providers[primary.provider]
    primary_model = ModelSettings(
        provider.api_key,
        provider.base_url,
        primary.model,
        provider.timeout_seconds,
        primary.input_cost_per_million,
        primary.output_cost_per_million,
    )
    raw_lsp_servers = group("lsp").get("servers", {})
    lsp_servers = {
        str(name): LspServerSettings(
            command=tuple(str(item) for item in server.get("command", ())),
            extensions=tuple(str(item) for item in server.get("extensions", ())),
            root_markers=tuple(
                str(item) for item in server.get("root_markers", (".git",))
            ),
        )
        for name, server in (
            raw_lsp_servers.items() if isinstance(raw_lsp_servers, dict) else ()
        )
        if isinstance(server, dict)
    }
    return settings_factory(
        model_config=primary_model,
        runtime=RuntimeSettings(
            max_tool_rounds=max_tool_rounds(group("runtime")),
        ),
        tools=ToolSettings(
            schema_budget_tokens=int(group("tools").get("schema_budget_tokens", 8_000)),
            max_read_concurrency=int(group("tools").get("max_read_concurrency", 4)),
            aggregate_result_bytes=int(group("tools").get("aggregate_result_bytes", 65_536)),
        ),
        shell=ShellSettings(
            enabled=boolean(group("shell").get("enabled", True)),
            default_timeout_seconds=float(group("shell").get("default_timeout_seconds", 120)),
            max_timeout_seconds=float(group("shell").get("max_timeout_seconds", 600)),
            classifier_enabled=boolean(group("shell").get("classifier_enabled", True)),
            classifier_threshold=float(group("shell").get("classifier_threshold", 0.95)),
            background_enabled=boolean(group("shell").get("background_enabled", True)),
            output_bytes=int(group("shell").get("output_bytes", 100_000)),
        ),
        context=ContextSettings(
            auto_compact=boolean(group("context").get("auto_compact", True)),
            trigger_ratio=float(group("context").get("trigger_ratio", 0.80)),
            target_ratio=float(group("context").get("target_ratio", 0.60)),
            preserve_recent_turns=int(
                group("context").get("preserve_recent_turns", 6)
            ),
            inline_tool_result_bytes=int(
                group("context").get("inline_tool_result_bytes", 16_384)
            ),
            summary_max_tokens=int(
                group("context").get("summary_max_tokens", 2_048)
            ),
            max_compaction_failures=int(
                group("context").get("max_compaction_failures", 3)
            ),
        ),
        agents=agent_settings(group("agents")),
        lsp=LspSettings(
            enabled=boolean(group("lsp").get("enabled", True)),
            startup_timeout_seconds=float(
                group("lsp").get("startup_timeout_seconds", 10)
            ),
            request_timeout_seconds=float(
                group("lsp").get("request_timeout_seconds", 15)
            ),
            idle_timeout_seconds=float(
                group("lsp").get("idle_timeout_seconds", 300)
            ),
            servers=lsp_servers,
        ),
        documents=DocumentSettings(
            max_pdf_bytes=int(
                group("documents").get("max_pdf_bytes", 50 * 1024 * 1024)
            ),
            max_pdf_pages=int(group("documents").get("max_pdf_pages", 10)),
            max_notebook_bytes=int(
                group("documents").get("max_notebook_bytes", 10 * 1024 * 1024)
            ),
            max_notebook_cells=int(
                group("documents").get("max_notebook_cells", 50)
            ),
            max_cell_output_bytes=int(
                group("documents").get("max_cell_output_bytes", 65_536)
            ),
        ),
        worktree=WorktreeSettings(
            enabled=boolean(group("worktree").get("enabled", True)),
            max_per_session=int(group("worktree").get("max_per_session", 4)),
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
            tavily_api_key=web_credential(
                group("web"),
                value("web", "CAPSLOCK_TAVILY_API_KEY", None),
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
