"""Configuration with environment variables taking precedence over TOML."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: float
    max_turns: int
    max_context_messages: int
    command_timeout_seconds: float
    command_output_bytes: int
    input_cost_per_million: float
    output_cost_per_million: float

    @classmethod
    def load(cls, workspace: Path) -> "Settings":
        values: dict[str, object] = {}
        config = workspace / "capslock.toml"
        if config.is_file():
            values = tomllib.loads(config.read_text(encoding="utf-8")).get("model", {})
        def value(name: str, default: object) -> object:
            return os.environ.get(name, values.get(name.lower().replace("capslock_", ""), default))
        return cls(
            api_key=os.environ.get("CAPSLOCK_API_KEY", os.environ.get("DEEPSEEK_API_KEY") or values.get("api_key")),
            base_url=str(value("CAPSLOCK_BASE_URL", os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))),
            model=str(value("CAPSLOCK_MODEL", os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"))),
            timeout_seconds=float(value("CAPSLOCK_TIMEOUT_SECONDS", 60)),
            max_turns=int(value("CAPSLOCK_MAX_TURNS", 6)),
            max_context_messages=int(value("CAPSLOCK_MAX_CONTEXT_MESSAGES", 24)),
            command_timeout_seconds=float(value("CAPSLOCK_COMMAND_TIMEOUT_SECONDS", 120)),
            command_output_bytes=int(value("CAPSLOCK_COMMAND_OUTPUT_BYTES", 100_000)),
            input_cost_per_million=float(value("CAPSLOCK_INPUT_COST_PER_MILLION", 0)),
            output_cost_per_million=float(value("CAPSLOCK_OUTPUT_COST_PER_MILLION", 0)),
        )
