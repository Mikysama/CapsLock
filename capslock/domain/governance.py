"""Run limits, budget snapshots, and structured stop reasons."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class RunMode(StrEnum):
    INTERACTIVE = "interactive"
    EXEC = "exec"


class StopReason(StrEnum):
    MAX_TOOL_ROUNDS = "max_tool_rounds"
    MAX_TOOL_CALLS = "max_tool_calls"
    MAX_DURATION = "max_duration"
    MAX_TOKENS = "max_tokens"
    MAX_BUDGET_USD = "max_budget_usd"
    REPEATED_TOOL_CALL = "repeated_tool_call"


@dataclass(frozen=True)
class RunLimits:
    max_tool_rounds: int = 32
    max_tool_calls: int | None = None
    max_duration_seconds: float | None = None
    max_tokens: int | None = None
    max_budget_usd: float | None = None

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")

    def as_dict(self) -> dict[str, int | float | None]:
        return asdict(self)


@dataclass(frozen=True)
class LoopDetectionSettings:
    consecutive_repeats: int = 3
    failed_retries: int = 3
    cycle_repetitions: int = 3
    max_cycle_length: int = 4

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value < 2:
                raise ValueError(f"loop_detection.{name} must be at least 2")


@dataclass(frozen=True)
class BudgetSnapshot:
    mode: RunMode
    root_run_id: str
    limits: RunLimits
    tool_rounds: int = 0
    tool_calls: int = 0
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0
    extensions: int = 0
    stop_reason: StopReason | None = None

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, Any]:
        limits = self.limits.as_dict()
        used = {
            "tool_rounds": self.tool_rounds,
            "tool_calls": self.tool_calls,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "budget_usd": self.cost_usd,
        }
        remaining = {
            "tool_rounds": max(0, self.limits.max_tool_rounds - self.tool_rounds),
            "tool_calls": _remaining(self.limits.max_tool_calls, self.tool_calls),
            "duration_ms": _remaining_seconds(
                self.limits.max_duration_seconds, self.duration_ms
            ),
            "tokens": _remaining(self.limits.max_tokens, self.tokens),
            "budget_usd": _remaining_float(self.limits.max_budget_usd, self.cost_usd),
        }
        return {
            "mode": self.mode.value,
            "root_run_id": self.root_run_id,
            "limits": limits,
            "used": used,
            "remaining": remaining,
            "extensions": self.extensions,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
        }


def _remaining(limit: int | None, used: int) -> int | None:
    return None if limit is None else max(0, limit - used)


def _remaining_seconds(limit: float | None, used_ms: int) -> int | None:
    return None if limit is None else max(0, round(limit * 1000) - used_ms)


def _remaining_float(limit: float | None, used: float) -> float | None:
    return None if limit is None else max(0.0, limit - used)


class RunStopped(RuntimeError):
    def __init__(
        self,
        reason: StopReason,
        snapshot: BudgetSnapshot,
        *,
        detail: dict[str, Any] | None = None,
        summarize: bool = False,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.snapshot = snapshot
        self.detail = detail or {}
        self.summarize = summarize
