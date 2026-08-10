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
from .types import ConfigIssue


CONFIG_VERSION = 9
_GROUP_FIELDS = {
    "runtime": {"max_tool_rounds", "permission_mode"},
    "tools": {"schema_budget_tokens", "max_read_concurrency", "aggregate_result_bytes"},
    "shell": {
        "enabled",
        "default_timeout_seconds",
        "max_timeout_seconds",
        "classifier_enabled",
        "classifier_threshold",
        "background_enabled",
        "output_bytes",
    },
    "context": {
        "auto_compact",
        "trigger_ratio",
        "target_ratio",
        "preserve_recent_turns",
        "inline_tool_result_bytes",
        "summary_max_tokens",
        "max_compaction_failures",
        "tokenizer",
        "episodic_recall_enabled",
        "episodic_recall_limit",
        "episodic_recall_bytes",
    },
    "agents": {
        "enabled",
        "max_children",
        "max_concurrency",
        "max_depth",
        "max_child_tool_rounds",
        "background_enabled",
        "mailbox_enabled",
        "message_ttl_seconds",
    },
    "lsp": {
        "enabled",
        "startup_timeout_seconds",
        "request_timeout_seconds",
        "idle_timeout_seconds",
        "servers",
    },
    "documents": {
        "max_pdf_bytes",
        "max_pdf_pages",
        "max_notebook_bytes",
        "max_notebook_cells",
        "max_cell_output_bytes",
    },
    "worktree": {"enabled", "max_per_session"},
    "command": {"command_timeout_seconds", "command_output_bytes"},
    "web": {
        "tavily_api_key",
        "tavily_credential",
        "web_timeout_seconds",
        "web_max_bytes",
        "web_max_redirects",
    },
    "mcp": {"mcp_timeout_seconds", "mcp_output_bytes", "remote_enabled"},
    "bridge": {"enabled", "max_selection_bytes", "max_diagnostics"},
    "observability": {"enabled", "retention_days", "max_spans"},
    "memory": {
        "capture_enabled",
        "recall_enabled",
        "manual_write_enabled",
        "maintenance_enabled",
        "policy",
        "temporary_ttl_days",
    },
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
    elif version != CONFIG_VERSION:
        issues.append(
            ConfigIssue(
                "error",
                "config_version_unsupported",
                "config_version",
                f"unsupported configuration version {version}",
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
                    "error",
                    "unknown_field",
                    f"{group_name}.{field}",
                    "unknown field",
                )
            )
        secret_fields = {"tavily_api_key"} if group_name == "web" else set()
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
    model_routes(document, resolve_credentials=False)
    budget_settings(
        document.get("budget", {}) if isinstance(document.get("budget"), dict) else {}
    )
    agents = document.get("agents", {})
    if isinstance(agents, dict):
        agent_settings(agents)
    lsp = document.get("lsp", {})
    if isinstance(lsp, dict):
        boolean(lsp.get("enabled", True))
        for field, default in (
            ("startup_timeout_seconds", 10),
            ("request_timeout_seconds", 15),
            ("idle_timeout_seconds", 300),
        ):
            if float(lsp.get(field, default)) <= 0:
                raise ValueError(f"lsp.{field} must be positive")
        servers = lsp.get("servers", {})
        if not isinstance(servers, dict):
            raise ValueError("lsp.servers must be a table")
        for name, server in servers.items():
            if not isinstance(server, dict):
                raise ValueError(f"lsp.servers.{name} must be a table")
            unknown = set(server) - {"command", "extensions", "root_markers"}
            if unknown:
                raise ValueError(f"unknown lsp server field: {sorted(unknown)[0]}")
            if not isinstance(server.get("command"), list) or not server["command"]:
                raise ValueError(
                    f"lsp.servers.{name}.command must be a non-empty array"
                )
            if (
                not isinstance(server.get("extensions"), list)
                or not server["extensions"]
            ):
                raise ValueError(
                    f"lsp.servers.{name}.extensions must be a non-empty array"
                )
    documents = document.get("documents", {})
    if isinstance(documents, dict):
        for field, default in (
            ("max_pdf_bytes", 50 * 1024 * 1024),
            ("max_pdf_pages", 10),
            ("max_notebook_bytes", 10 * 1024 * 1024),
            ("max_notebook_cells", 50),
            ("max_cell_output_bytes", 65_536),
        ):
            if int(documents.get(field, default)) <= 0:
                raise ValueError(f"documents.{field} must be positive")
    worktree = document.get("worktree", {})
    if isinstance(worktree, dict):
        boolean(worktree.get("enabled", True))
        if not 1 <= int(worktree.get("max_per_session", 4)) <= 32:
            raise ValueError("worktree.max_per_session must be between 1 and 32")
    runtime = document.get("runtime", {})
    if isinstance(runtime, dict):
        if int(runtime.get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS)) <= 0:
            raise ValueError("runtime.max_tool_rounds must be positive")
    tools = document.get("tools", {})
    if isinstance(tools, dict):
        if int(tools.get("schema_budget_tokens", 8_000)) <= 0:
            raise ValueError("tools.schema_budget_tokens must be positive")
        concurrency = int(tools.get("max_read_concurrency", 4))
        if concurrency < 1 or concurrency > 32:
            raise ValueError("tools.max_read_concurrency must be between 1 and 32")
        if int(tools.get("aggregate_result_bytes", 65_536)) < 1024:
            raise ValueError("tools.aggregate_result_bytes must be at least 1024")
    shell = document.get("shell", {})
    if isinstance(shell, dict):
        default_timeout = float(shell.get("default_timeout_seconds", 120))
        max_timeout = float(shell.get("max_timeout_seconds", 600))
        threshold = float(shell.get("classifier_threshold", 0.95))
        if default_timeout <= 0 or max_timeout < default_timeout:
            raise ValueError("shell timeout limits are invalid")
        if threshold < 0.95 or threshold > 1:
            raise ValueError("shell.classifier_threshold must be between 0.95 and 1")
        if int(shell.get("output_bytes", 100_000)) <= 0:
            raise ValueError("shell.output_bytes must be positive")
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
        for field in (
            "capture_enabled",
            "recall_enabled",
            "manual_write_enabled",
            "maintenance_enabled",
        ):
            boolean(memory.get(field, True))
        if str(memory.get("policy", "automatic")) not in {
            "off",
            "review",
            "automatic",
        }:
            raise ValueError("memory.policy must be off, review, or automatic")
        if not 1 <= int(memory.get("temporary_ttl_days", 7)) <= 365:
            raise ValueError("memory.temporary_ttl_days must be between 1 and 365")
    context = document.get("context", {})
    if isinstance(context, dict):
        boolean(context.get("auto_compact", True))
        trigger = float(context.get("trigger_ratio", 0.80))
        target = float(context.get("target_ratio", 0.60))
        if not 0 < target < trigger <= 1:
            raise ValueError(
                "context ratios must satisfy 0 < target_ratio < trigger_ratio <= 1"
            )
        for field, default in (
            ("preserve_recent_turns", 6),
            ("inline_tool_result_bytes", 16_384),
            ("summary_max_tokens", 2_048),
            ("max_compaction_failures", 3),
            ("episodic_recall_limit", 5),
            ("episodic_recall_bytes", 4_096),
        ):
            if int(context.get(field, default)) <= 0:
                raise ValueError(f"context.{field} must be positive")
        boolean(context.get("episodic_recall_enabled", True))
        if int(context.get("episodic_recall_limit", 5)) > 20:
            raise ValueError("context.episodic_recall_limit must not exceed 20")
        tokenizer = str(context.get("tokenizer", "adaptive"))
        if tokenizer != "adaptive" and not tokenizer.startswith("tiktoken:") and tokenizer != "heuristic":
            raise ValueError("context.tokenizer must be adaptive, heuristic, or tiktoken:<encoding>")
    bridge = document.get("bridge", {})
    if isinstance(bridge, dict):
        boolean(bridge.get("enabled", False))
        if int(bridge.get("max_selection_bytes", 65_536)) < 1024:
            raise ValueError("bridge.max_selection_bytes must be at least 1024")
        if not 1 <= int(bridge.get("max_diagnostics", 500)) <= 5000:
            raise ValueError("bridge.max_diagnostics must be between 1 and 5000")
    observability = document.get("observability", {})
    if isinstance(observability, dict):
        boolean(observability.get("enabled", True))
        if int(observability.get("retention_days", 30)) < 1:
            raise ValueError("observability.retention_days must be positive")
        if int(observability.get("max_spans", 100_000)) < 100:
            raise ValueError("observability.max_spans must be at least 100")
    loop_detection = document.get("loop_detection", {})
    if isinstance(loop_detection, dict):
        loop_detection_settings(loop_detection)
