"""Typed configuration values exposed to the application."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = True
    max_children: int = 4
    max_concurrency: int = 2
    max_depth: int = 1
    max_child_tool_rounds: int = 16


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
class ConfigIssue:
    severity: str
    code: str
    path: str
    message: str
