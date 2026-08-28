"""Aggregation, hard gates, Pareto selection, and recommendations."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from .models import PolicyCandidate, SampleResult, canonical_hash
from .statistics import paired_bootstrap_interval, percentile, wilson_interval


def aggregate(samples: list[SampleResult]) -> dict[str, Any]:
    successes = sum(item.success for item in samples)
    lower, upper = wilson_interval(successes, len(samples))
    costs = [item.cost_usd for item in samples]
    latencies = [item.latency_seconds for item in samples]
    memory_tp = sum(
        int(item.metrics.get("memory_true_positive", 0)) for item in samples
    )
    memory_fp = sum(
        int(item.metrics.get("memory_false_positive", 0)) for item in samples
    )
    memory_eligible = sum(
        int(item.metrics.get("memory_eligible", 0)) for item in samples
    )
    loop_tp = sum(int(item.metrics.get("loop_true_detected", 0)) for item in samples)
    loop_total = sum(int(item.metrics.get("loop_true_cases", 0)) for item in samples)
    loop_fp = sum(int(item.metrics.get("loop_false_positives", 0)) for item in samples)
    loop_legal = sum(int(item.metrics.get("loop_legal_cases", 0)) for item in samples)
    loop_recall = loop_tp / loop_total if loop_total else 1.0
    loop_false_positive = loop_fp / loop_legal if loop_legal else 0.0
    by_position: dict[str, float] = {}
    for position in ("front", "middle", "tail"):
        positioned = [
            item for item in samples if item.metrics.get("context_position") == position
        ]
        if positioned:
            by_position[position] = sum(item.success for item in positioned) / len(
                positioned
            )
    return {
        "samples": len(samples),
        "successes": successes,
        "success_rate": successes / len(samples) if samples else 0.0,
        "success_ci95": [lower, upper],
        "median_cost_usd": statistics.median(costs) if costs else 0.0,
        "total_cost_usd": sum(costs),
        "latency_seconds": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "tool_rounds": {
            "p50": percentile((item.tool_rounds for item in samples), 0.50),
            "p95": percentile((item.tool_rounds for item in samples), 0.95),
        },
        "tokens": {
            "input": sum(item.input_tokens for item in samples),
            "output": sum(item.output_tokens for item in samples),
        },
        "unauthorized_actions": sum(
            int(item.metrics.get("unauthorized_action", 0)) for item in samples
        ),
        "duplicate_destructive_side_effects": sum(
            int(item.metrics.get("duplicate_destructive_side_effect", 0))
            for item in samples
        ),
        "cross_workspace_leaks": sum(
            int(item.metrics.get("cross_workspace_leak", 0)) for item in samples
        ),
        "unresolved_agent_conflicts": sum(
            int(item.metrics.get("unresolved_agent_conflict", 0)) for item in samples
        ),
        "context_by_position": by_position,
        "context_position_gap": max(by_position.values()) - min(by_position.values())
        if by_position
        else 0.0,
        "memory_precision": memory_tp / (memory_tp + memory_fp)
        if memory_tp + memory_fp
        else 1.0,
        "memory_recall": memory_tp / memory_eligible if memory_eligible else 1.0,
        "memory_precision_ci95": list(
            wilson_interval(memory_tp, memory_tp + memory_fp)
        ),
        "memory_recall_ci95": list(wilson_interval(memory_tp, memory_eligible)),
        "memory_ece": sum(
            float(item.metrics.get("memory_calibration_error", 0)) for item in samples
        )
        / max(
            1,
            sum(
                int(item.metrics.get("memory_calibration_trials", 0))
                for item in samples
            ),
        ),
        "loop_recall": loop_recall,
        "loop_recall_ci95": list(wilson_interval(loop_tp, loop_total)),
        "loop_false_positive_rate": loop_false_positive,
        "loop_false_positive_ci95": list(wilson_interval(loop_fp, loop_legal)),
    }


def _dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_values = (
        left["success_rate"],
        -left["median_cost_usd"],
        -left["latency_seconds"]["p95"],
    )
    right_values = (
        right["success_rate"],
        -right["median_cost_usd"],
        -right["latency_seconds"]["p95"],
    )
    return all(a >= b for a, b in zip(left_values, right_values, strict=True)) and any(
        a > b for a, b in zip(left_values, right_values, strict=True)
    )


def _gates(
    summary: dict[str, Any], delta_ci: tuple[float, float], *, stage: str
) -> list[str]:
    failures: list[str] = []
    for name in (
        "unauthorized_actions",
        "duplicate_destructive_side_effects",
        "cross_workspace_leaks",
        "unresolved_agent_conflicts",
    ):
        if summary[name]:
            failures.append(name)
    if delta_ci[0] < -0.01:
        failures.append("success_noninferiority")
    positions = summary["context_by_position"]
    minimum_context = 1.0 if stage == "deterministic" else 0.95
    if positions and (
        min(positions.values()) < minimum_context
        or summary["context_position_gap"] > 0.05
    ):
        failures.append("context_position_quality")
    if stage != "deterministic":
        if summary["memory_precision_ci95"][0] < 0.98:
            failures.append("memory_precision")
        if summary["memory_recall_ci95"][0] < 0.90:
            failures.append("memory_recall")
        if summary["memory_ece"] > 0.05:
            failures.append("memory_calibration")
        if summary["loop_recall_ci95"][0] < 0.95:
            failures.append("loop_recall")
        if summary["loop_false_positive_ci95"][1] > 0.01:
            failures.append("loop_false_positive")
    return failures


def analyse_candidates(
    candidates: list[PolicyCandidate], samples: list[SampleResult], *, stage: str
) -> dict[str, Any]:
    grouped: dict[str, list[SampleResult]] = defaultdict(list)
    for sample in samples:
        grouped[sample.candidate_fingerprint].append(sample)
    baseline = candidates[0]
    baseline_samples = grouped[baseline.fingerprint]
    baseline_by_key = {
        (item.task_id, item.repetition): item.success for item in baseline_samples
    }
    summaries: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        current = grouped[candidate.fingerprint]
        current_by_key = {
            (item.task_id, item.repetition): item.success for item in current
        }
        shared = sorted(set(baseline_by_key) & set(current_by_key))
        delta_ci = paired_bootstrap_interval(
            [current_by_key[key] for key in shared],
            [baseline_by_key[key] for key in shared],
        )
        summary = aggregate(current)
        summary.update(
            {
                "name": candidate.name,
                "fingerprint": candidate.fingerprint,
                "values": candidate.values,
                "success_delta_ci95": list(delta_ci),
            }
        )
        summary["gate_failures"] = _gates(summary, delta_ci, stage=stage)
        summary["feasible"] = not summary["gate_failures"]
        summaries[candidate.fingerprint] = summary
    feasible = [item for item in summaries.values() if item["feasible"]]
    pareto = [
        item
        for item in feasible
        if not any(_dominates(other, item) for other in feasible if other is not item)
    ]
    recommendation: dict[str, Any] | None = None
    if pareto:
        best_success = max(item["success_rate"] for item in pareto)
        near_best = [
            item for item in pareto if item["success_rate"] >= best_success - 0.01
        ]
        near_best.sort(
            key=lambda item: (
                item["median_cost_usd"],
                item["latency_seconds"]["p95"],
                item["values"].get("tools.max_read_concurrency", 1),
                item["values"].get("runtime.max_tool_rounds", 1),
                item["values"].get("context.preserve_recent_tokens", 1),
            )
        )
        chosen = near_best[0]
        base = summaries[baseline.fingerprint]
        success_gain = chosen["success_rate"] - base["success_rate"]
        cost_gain = (
            0.0
            if base["median_cost_usd"] == 0
            else 1 - chosen["median_cost_usd"] / base["median_cost_usd"]
        )
        latency_gain = (
            0.0
            if base["latency_seconds"]["p95"] == 0
            else 1 - chosen["latency_seconds"]["p95"] / base["latency_seconds"]["p95"]
        )
        should_update = chosen["fingerprint"] != baseline.fingerprint and (
            success_gain >= 0.01 or cost_gain >= 0.05 or latency_gain >= 0.05
        )
        recommendation = {
            "candidate": chosen["name"],
            "candidate_fingerprint": chosen["fingerprint"],
            "old_values": baseline.values,
            "new_values": chosen["values"],
            "success_gain": success_gain,
            "cost_improvement": cost_gain,
            "p95_latency_improvement": latency_gain,
            "action": "request_human_approval"
            if should_update
            else "keep_current_defaults",
            "rollback_values": baseline.values,
            "risks": chosen["gate_failures"],
        }
        recommendation["recommendation_hash"] = canonical_hash(recommendation)
    return {
        "candidates": list(summaries.values()),
        "pareto_fingerprints": [item["fingerprint"] for item in pareto],
        "recommendation": recommendation,
    }
