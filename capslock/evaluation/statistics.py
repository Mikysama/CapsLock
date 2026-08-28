"""Dependency-free statistics used by evaluation reports."""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = min(1.0, max(0.0, quantile)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def wilson_interval(
    successes: int, total: int, *, z: float = 1.95996398454
) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    rate = successes / total
    denominator = 1 + z * z / total
    centre = (rate + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total))
        / denominator
    )
    return max(0.0, centre - margin), min(1.0, centre + margin)


def paired_bootstrap_interval(
    candidate: Sequence[bool],
    baseline: Sequence[bool],
    *,
    samples: int = 10_000,
    seed: int = 20260828,
) -> tuple[float, float]:
    if len(candidate) != len(baseline):
        raise ValueError("paired samples must have equal length")
    if not candidate:
        return 0.0, 0.0
    differences = [
        float(left) - float(right)
        for left, right in zip(candidate, baseline, strict=True)
    ]
    generator = random.Random(seed)
    estimates = [
        sum(differences[generator.randrange(len(differences))] for _ in differences)
        / len(differences)
        for _ in range(samples)
    ]
    return percentile(estimates, 0.025), percentile(estimates, 0.975)
