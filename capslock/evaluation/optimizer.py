"""Seeded Gaussian-process search for shortlisted Memory ranking weights."""

from __future__ import annotations

import math
import random
from typing import Any

from .models import PolicyCandidate
from .registry import baseline_values

WEIGHT_PATHS = (
    "memory.retrieval_weight",
    "memory.scope_weight",
    "memory.confidence_weight",
    "memory.freshness_weight",
    "memory.source_validity_weight",
)


def _vector(values: dict[str, int | float]) -> tuple[float, ...]:
    return tuple(float(values[path]) for path in WEIGHT_PATHS)


def _distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def _kernel(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    distance = _distance(left, right)
    return math.exp(-(distance**2) / (2 * 0.25**2))


def _cholesky(matrix: list[list[float]]) -> list[list[float]]:
    result = [[0.0] * len(matrix) for _ in matrix]
    for row in range(len(matrix)):
        for column in range(row + 1):
            residual = matrix[row][column] - sum(
                result[row][index] * result[column][index] for index in range(column)
            )
            result[row][column] = (
                math.sqrt(max(residual, 1e-12))
                if row == column
                else residual / result[column][column]
            )
    return result


def _forward(matrix: list[list[float]], values: list[float]) -> list[float]:
    result: list[float] = []
    for row in range(len(values)):
        residual = values[row] - sum(
            matrix[row][column] * result[column] for column in range(row)
        )
        result.append(residual / matrix[row][row])
    return result


def _backward(matrix: list[list[float]], values: list[float]) -> list[float]:
    result = [0.0] * len(values)
    for row in range(len(values) - 1, -1, -1):
        residual = values[row] - sum(
            matrix[column][row] * result[column]
            for column in range(row + 1, len(values))
        )
        result[row] = residual / matrix[row][row]
    return result


def _posterior(
    observed: list[tuple[tuple[float, ...], float]],
    vector: tuple[float, ...],
) -> tuple[float, float]:
    covariance = [
        [
            _kernel(left, right) + (1e-6 if row == column else 0.0)
            for column, (right, _) in enumerate(observed)
        ]
        for row, (left, _) in enumerate(observed)
    ]
    factor = _cholesky(covariance)
    outcomes = [outcome for _, outcome in observed]
    alpha = _backward(factor, _forward(factor, outcomes))
    covariance_to_point = [_kernel(prior, vector) for prior, _ in observed]
    mean = sum(
        value * coefficient
        for value, coefficient in zip(covariance_to_point, alpha, strict=True)
    )
    projected = _forward(factor, covariance_to_point)
    variance = max(1e-9, 1.0 - sum(value * value for value in projected))
    return mean, variance


def propose_memory_weights(
    base: PolicyCandidate,
    observations: list[dict[str, Any]],
    *,
    count: int = 16,
    seed: int = 20260828,
) -> list[PolicyCandidate]:
    """Propose simplex weights with a Gaussian-process upper-confidence bound."""
    generator = random.Random(seed)
    buckets: dict[tuple[float, ...], list[float]] = {}
    for item in observations:
        if all(path in item.get("values", {}) for path in WEIGHT_PATHS):
            buckets.setdefault(_vector(item["values"]), []).append(
                float(item.get("quality_success_rate", item["success_rate"]))
            )
    observed = [
        (vector, sum(outcomes) / len(outcomes)) for vector, outcomes in buckets.items()
    ]
    pool: list[tuple[float, tuple[float, ...]]] = []
    default = _vector(baseline_values())
    for _ in range(max(256, count * 32)):
        raw = [generator.expovariate(1.0) for _ in WEIGHT_PATHS]
        total = sum(raw)
        vector = tuple(round(value / total, 6) for value in raw)
        vector = (*vector[:-1], round(1 - sum(vector[:-1]), 6))
        if observed:
            prediction, variance = _posterior(observed, vector)
            acquisition = prediction + 1.5 * math.sqrt(variance)
        else:
            acquisition = _distance(vector, default)
        pool.append((acquisition, vector))
    pool.sort(reverse=True)
    candidates: list[PolicyCandidate] = []
    seen: set[tuple[float, ...]] = set()
    for _, vector in pool:
        if vector in seen:
            continue
        seen.add(vector)
        values = {**base.values, **dict(zip(WEIGHT_PATHS, vector, strict=True))}
        candidates.append(
            PolicyCandidate(f"memory-weights-{len(candidates) + 1}", values)
        )
        if len(candidates) == count:
            break
    return candidates
