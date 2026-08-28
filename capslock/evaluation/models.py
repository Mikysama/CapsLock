"""Stable data contracts for policy experiments and reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PolicyCandidate:
    name: str
    values: dict[str, int | float]

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class EvaluationTask:
    id: str
    subsystem: str
    split: str
    prompt: str
    requirements: dict[str, Any]
    critical: bool = False


@dataclass(frozen=True)
class SampleResult:
    task_id: str
    subsystem: str
    candidate: str
    candidate_fingerprint: str
    repetition: int
    seed: int
    success: bool
    stop_reason: str | None
    latency_seconds: float
    tool_rounds: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    metrics: dict[str, float | int | bool | str] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()
