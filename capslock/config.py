"""Configuration with environment variables taking precedence over TOML."""

from __future__ import annotations

import os
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

        return cls(
            model_config=ModelSettings(
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
            ),
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
