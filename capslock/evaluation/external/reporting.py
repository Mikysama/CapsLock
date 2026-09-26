"""Per-suite external benchmark reports and paired comparisons."""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import fmean, median
from typing import Any

from ..statistics import paired_bootstrap_interval, percentile, wilson_interval
from .contracts import ExternalTask, TaskResult, canonical_hash


def build_report(
    results: list[TaskResult], tasks: list[ExternalTask]
) -> dict[str, Any]:
    task_index = {task.instance_id: task for task in tasks}
    if any(item.instance_id not in task_index for item in results):
        raise ValueError("results contain tasks absent from the catalog")
    resolved = sum(item.resolved for item in results)
    total = len(results)
    valid = [
        item
        for item in results
        if not item.infrastructure_error
        and item.grader_status not in {"infrastructure_error", "not_run"}
    ]
    valid_resolved = sum(item.resolved for item in valid)
    by_instance: dict[str, list[bool]] = defaultdict(list)
    for item in results:
        by_instance[item.instance_id].append(item.resolved)
    triple = [values for values in by_instance.values() if len(values) >= 3]
    total_tokens = [item.input_tokens + item.output_tokens for item in results]
    costs = [item.cost_usd for item in results]
    durations = [item.duration_seconds for item in results]
    tool_calls = [item.tool_calls for item in results]
    peak_context_tokens = [item.peak_context_tokens for item in results]
    context_compactions = [item.context_compactions for item in results]
    report: dict[str, Any] = {
        "schema_version": 1,
        "suite": results[0].suite if results else "unknown",
        "model_track": results[0].model_track if results else "unknown",
        "sample_count": total,
        "valid_attempt_count": len(valid),
        "infrastructure_failure_count": total - len(valid),
        "valid_resolve_rate": valid_resolved / len(valid) if valid else None,
        "human_interventions": sum(item.human_interventions or 0 for item in valid)
        if valid and all(item.human_interventions is not None for item in valid)
        else None,
        "intervention_measurement_count": sum(
            item.human_interventions is not None for item in valid
        ),
        "cost_per_successful_task_usd": (
            sum(item.cost_usd for item in valid) / valid_resolved
            if valid_resolved
            else None
        ),
        "task_count": len(by_instance),
        "resolved": resolved,
        "resolve_rate": resolved / total if total else 0.0,
        "resolve_ci95": list(wilson_interval(resolved, total)),
        "reliable_success_3": (
            sum(all(values[:3]) for values in triple) / len(triple) if triple else None
        ),
        "any_success_3": (
            sum(any(values[:3]) for values in triple) / len(triple) if triple else None
        ),
        "median_duration_seconds": median(durations) if results else 0.0,
        "p95_duration_seconds": percentile(durations, 0.95),
        "median_tokens": median(total_tokens) if results else 0.0,
        "p95_tokens": percentile(total_tokens, 0.95),
        "average_cost_usd": fmean(costs) if results else 0.0,
        "median_cost_usd": median(costs) if results else 0.0,
        "p95_cost_usd": percentile(costs, 0.95),
        "median_tool_calls": median(tool_calls) if results else 0.0,
        "p95_tool_calls": percentile(tool_calls, 0.95),
        "median_peak_context_tokens": median(peak_context_tokens) if results else 0.0,
        "p95_peak_context_tokens": percentile(peak_context_tokens, 0.95),
        "total_context_compactions": sum(context_compactions),
        "failure_categories": dict(
            sorted(
                Counter(
                    item.failure_category or "uncategorized"
                    for item in results
                    if not item.resolved
                ).items()
            )
        ),
        "by_language": _dimension(results, task_index, "language"),
        "by_repository": _dimension(results, task_index, "repository"),
        "by_task_type": _dimension(results, task_index, "task_type"),
        "by_resource_class": _dimension(results, task_index, "resource_class"),
        "infrastructure_valid": not any(
            item.infrastructure_error
            or item.grader_status in {"infrastructure_error", "not_run"}
            for item in results
        ),
    }
    report["report_hash"] = canonical_hash(report)
    return report


def compare_reports(
    candidate: list[TaskResult], baseline: list[TaskResult], *, seed: int
) -> dict[str, Any]:
    candidate_identity = {(item.suite, item.model_track) for item in candidate}
    baseline_identity = {(item.suite, item.model_track) for item in baseline}
    if len(candidate_identity) != 1 or candidate_identity != baseline_identity:
        raise ValueError("paired comparison requires the same suite and model track")
    left = {(item.instance_id, item.run_ordinal): item for item in candidate}
    right = {(item.instance_id, item.run_ordinal): item for item in baseline}
    if len(left) != len(candidate) or len(right) != len(baseline):
        raise ValueError("paired comparison contains duplicate task ordinals")
    keys = sorted(set(left) & set(right))
    if not keys:
        raise ValueError("candidate and baseline have no paired task results")
    left_values = [left[key].resolved for key in keys]
    right_values = [right[key].resolved for key in keys]
    interval = paired_bootstrap_interval(left_values, right_values, seed=seed)
    delta = sum(left_values) / len(keys) - sum(right_values) / len(keys)
    candidate_p95_duration = percentile(
        [left[key].duration_seconds for key in keys], 0.95
    )
    baseline_p95_duration = percentile(
        [right[key].duration_seconds for key in keys], 0.95
    )
    candidate_average_cost = fmean(left[key].cost_usd for key in keys)
    baseline_average_cost = fmean(right[key].cost_usd for key in keys)
    duration_regression = _regression(candidate_p95_duration, baseline_p95_duration)
    cost_regression = _regression(candidate_average_cost, baseline_average_cost)
    performance_regressed = _over_ten_percent(
        candidate_p95_duration, baseline_p95_duration
    ) or _over_ten_percent(candidate_average_cost, baseline_average_cost)
    meaningful_gain = interval[0] >= 0.02
    noninferior = interval[0] >= -0.01
    return {
        "paired_samples": len(keys),
        "resolve_delta": delta,
        "resolve_delta_ci95": list(interval),
        "noninferior_at_minus_1pp": noninferior,
        "meaningful_gain_at_2pp": meaningful_gain,
        "p95_duration_regression": duration_regression,
        "average_cost_regression": cost_regression,
        "performance_regressed_over_10pct": performance_regressed,
        "release_gate_passed": noninferior
        and (not performance_regressed or meaningful_gain),
    }


def _regression(candidate: float, baseline: float) -> float | None:
    if baseline == 0:
        return 0.0 if candidate == 0 else None
    return candidate / baseline - 1


def _over_ten_percent(candidate: float, baseline: float) -> bool:
    if baseline == 0:
        return candidate > 0
    return candidate / baseline - 1 > 0.10


def _dimension(
    results: list[TaskResult], tasks: dict[str, ExternalTask], attribute: str
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[TaskResult]] = defaultdict(list)
    for item in results:
        value = str(getattr(tasks[item.instance_id], attribute) or "unknown")
        grouped[value].append(item)
    return {
        key: {
            "samples": len(values),
            "resolved": sum(item.resolved for item in values),
            "resolve_rate": sum(item.resolved for item in values) / len(values),
        }
        for key, values in sorted(grouped.items())
    }
