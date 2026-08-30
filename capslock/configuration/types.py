"""Typed configuration values exposed to the application."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..behavior_defaults import (
    DEFAULT_AGENT_MAX_CHILD_TOOL_ROUNDS,
    DEFAULT_AGENT_MAX_CHILDREN,
    DEFAULT_AGENT_MAX_CONCURRENCY,
    DEFAULT_AGENT_MAX_DEPTH,
    DEFAULT_CONTEXT_MAX_COMPACTION_FAILURES,
    DEFAULT_CONTEXT_PRESERVE_RECENT_TOKENS,
    DEFAULT_CONTEXT_PRESERVE_RECENT_TURNS,
    DEFAULT_CONTEXT_TARGET_RATIO,
    DEFAULT_CONTEXT_TRIGGER_RATIO,
    DEFAULT_MAX_ARGUMENT_REPAIR_ATTEMPTS,
    DEFAULT_MAX_READ_CONCURRENCY,
    DEFAULT_MEMORY_RECALL_BYTES,
    DEFAULT_MEMORY_RECALL_LIMIT,
)


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
    api_key: str | None
    timeout_seconds: float
    data_policy: str
    credential_ref: str | None = None
    strict_tool_calls: bool = False


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


@dataclass(frozen=True)
class ToolSettings:
    schema_budget_tokens: int = 8_000
    max_read_concurrency: int = DEFAULT_MAX_READ_CONCURRENCY
    aggregate_result_bytes: int = 65_536
    selection_mode: str = "shadow"
    max_argument_repair_attempts: int = DEFAULT_MAX_ARGUMENT_REPAIR_ATTEMPTS


@dataclass(frozen=True)
class ShellSettings:
    enabled: bool = True
    default_timeout_seconds: float = 120
    max_timeout_seconds: float = 600
    classifier_enabled: bool = True
    classifier_threshold: float = 0.95
    background_enabled: bool = True
    output_bytes: int = 100_000


@dataclass(frozen=True)
class ContextSettings:
    auto_compact: bool = True
    trigger_ratio: float = DEFAULT_CONTEXT_TRIGGER_RATIO
    target_ratio: float = DEFAULT_CONTEXT_TARGET_RATIO
    preserve_recent_turns: int = DEFAULT_CONTEXT_PRESERVE_RECENT_TURNS
    preserve_recent_tokens: int = DEFAULT_CONTEXT_PRESERVE_RECENT_TOKENS
    working_set_limit: int = 5
    inline_tool_result_bytes: int = 16_384
    summary_max_tokens: int = 2_048
    max_compaction_failures: int = DEFAULT_CONTEXT_MAX_COMPACTION_FAILURES
    tokenizer: str = "adaptive"
    episodic_recall_enabled: bool = True
    episodic_recall_limit: int = DEFAULT_MEMORY_RECALL_LIMIT
    episodic_recall_bytes: int = DEFAULT_MEMORY_RECALL_BYTES


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = True
    max_children: int = DEFAULT_AGENT_MAX_CHILDREN
    max_concurrency: int = DEFAULT_AGENT_MAX_CONCURRENCY
    max_depth: int = DEFAULT_AGENT_MAX_DEPTH
    max_child_tool_rounds: int = DEFAULT_AGENT_MAX_CHILD_TOOL_ROUNDS
    background_enabled: bool = True
    mailbox_enabled: bool = True
    message_ttl_seconds: int = 3600
    default_workspace_mode: str = "snapshot"


@dataclass(frozen=True)
class LspServerSettings:
    command: tuple[str, ...]
    extensions: tuple[str, ...]
    root_markers: tuple[str, ...] = (".git",)


@dataclass(frozen=True)
class LspSettings:
    enabled: bool = True
    startup_timeout_seconds: float = 10
    request_timeout_seconds: float = 15
    idle_timeout_seconds: float = 300
    servers: dict[str, LspServerSettings] | None = None


@dataclass(frozen=True)
class DocumentSettings:
    max_pdf_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 10
    max_notebook_bytes: int = 10 * 1024 * 1024
    max_notebook_cells: int = 50
    max_cell_output_bytes: int = 65_536


@dataclass(frozen=True)
class WorktreeSettings:
    enabled: bool = True
    max_per_session: int = 4


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
    remote_enabled: bool = True


@dataclass(frozen=True)
class BridgeSettings:
    enabled: bool = False
    max_selection_bytes: int = 65_536
    max_diagnostics: int = 500


@dataclass(frozen=True)
class ObservabilitySettings:
    enabled: bool = True
    retention_days: int = 30
    max_spans: int = 100_000


@dataclass(frozen=True)
class MemorySettings:
    capture_enabled: bool = True
    recall_enabled: bool = True
    manual_write_enabled: bool = True
    maintenance_enabled: bool = True
    policy: str = "automatic"
    database: Path | None = None
    temporary_ttl_days: int = 7


@dataclass(frozen=True)
class ConfigIssue:
    severity: str
    code: str
    path: str
    message: str
