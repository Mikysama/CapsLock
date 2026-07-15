"""Configuration with environment variables taking precedence over TOML."""

from __future__ import annotations

import os
import tomllib
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
class Settings:
    model_config: ModelSettings
    runtime: RuntimeSettings
    command: CommandSettings
    web: WebSettings
    mcp: McpSettings
    permission_mode: str

    def __getattr__(self, name: str) -> object:
        for group in (self.model_config, self.runtime, self.command, self.web, self.mcp):
            if hasattr(group, name):
                return getattr(group, name)
        raise AttributeError(name)

    @classmethod
    def load(cls, workspace: Path) -> "Settings":
        values: dict[str, object] = {}
        config = workspace / "capslock.toml"
        if config.is_file():
            values = tomllib.loads(config.read_text(encoding="utf-8")).get("model", {})

        def value(name: str, default: object, *aliases: str) -> object:
            for environment_name in (name, *aliases):
                if environment_name in os.environ:
                    return os.environ[environment_name]
            config_name = name.lower().removeprefix("capslock_")
            return values.get(config_name, default)

        return cls(
            model_config=ModelSettings(
                api_key=value("CAPSLOCK_API_KEY", None, "DEEPSEEK_API_KEY"),
                base_url=str(value("CAPSLOCK_BASE_URL", "https://api.deepseek.com", "DEEPSEEK_BASE_URL")),
                model=str(value("CAPSLOCK_MODEL", "deepseek-v4-flash", "DEEPSEEK_MODEL")),
                timeout_seconds=float(value("CAPSLOCK_TIMEOUT_SECONDS", 60)),
                input_cost_per_million=float(value("CAPSLOCK_INPUT_COST_PER_MILLION", 0)),
                output_cost_per_million=float(value("CAPSLOCK_OUTPUT_COST_PER_MILLION", 0)),
            ),
            runtime=RuntimeSettings(
                max_turns=int(value("CAPSLOCK_MAX_TURNS", 6)),
                max_context_messages=int(value("CAPSLOCK_MAX_CONTEXT_MESSAGES", 24)),
            ),
            command=CommandSettings(
                command_timeout_seconds=float(value("CAPSLOCK_COMMAND_TIMEOUT_SECONDS", 120)),
                command_output_bytes=int(value("CAPSLOCK_COMMAND_OUTPUT_BYTES", 100_000)),
            ),
            web=WebSettings(
                tavily_api_key=value("CAPSLOCK_TAVILY_API_KEY", None, "TAVILY_API_KEY"),
                web_timeout_seconds=float(value("CAPSLOCK_WEB_TIMEOUT_SECONDS", 20)),
                web_max_bytes=int(value("CAPSLOCK_WEB_MAX_BYTES", 500_000)),
                web_max_redirects=int(value("CAPSLOCK_WEB_MAX_REDIRECTS", 3)),
            ),
            mcp=McpSettings(
                mcp_timeout_seconds=float(value("CAPSLOCK_MCP_TIMEOUT_SECONDS", 30)),
                mcp_output_bytes=int(value("CAPSLOCK_MCP_OUTPUT_BYTES", 100_000)),
            ),
            permission_mode=str(value("CAPSLOCK_PERMISSION_MODE", "approve_for_me")),
        )
