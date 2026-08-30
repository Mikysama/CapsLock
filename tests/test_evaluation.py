from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest
import jsonschema

from capslock.evaluation import (
    EvaluationRunner,
    build_tasks,
    load_matrix,
    propose_memory_weights,
)
from capslock.evaluation.models import PolicyCandidate, SampleResult
from capslock.evaluation.registry import baseline_values, registry_document
from capslock.evaluation.runner import (
    _answer_matches,
    _reasoning_request_options,
    _simulate,
    write_evaluation,
)
from capslock.evaluation.runtime_probe import _settings_for_candidate
from capslock.configuration import Settings
from capslock.evaluation.selection import analyse_candidates
from capslock.evaluation.statistics import (
    paired_bootstrap_interval,
    percentile,
    wilson_interval,
)

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "evaluations" / "core-v1.toml"


def _policy_script():
    spec = importlib.util.spec_from_file_location(
        "capslock_evaluate_policies", ROOT / "scripts" / "evaluate_policies.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_core_dataset_has_reviewed_split_sizes_and_no_overlap() -> None:
    tuning = build_tasks(split="tune")
    confirmation = build_tasks(split="confirm")
    assert len(tuning) == 220
    assert len(confirmation) == 60
    assert not ({item.id for item in tuning} & {item.id for item in confirmation})
    assert {item.subsystem for item in confirmation} == {
        "runtime",
        "context",
        "loop",
        "memory",
        "agents",
    }


def test_matrix_expands_stably_and_covers_registry() -> None:
    matrix = load_matrix(MATRIX)
    first = matrix.candidates()
    second = matrix.candidates()
    assert len(first) == 53
    assert [item.fingerprint for item in first] == [item.fingerprint for item in second]
    assert first[0].name == "baseline"
    assert set(matrix.parameters) <= set(baseline_values())
    assert any(item["safety_hard_limit"] for item in registry_document())


def test_matrix_fingerprint_includes_pricing() -> None:
    matrix = load_matrix(MATRIX)
    repriced = replace(
        matrix,
        input_cost_per_million=matrix.input_cost_per_million + 0.01,
    )
    assert matrix.fingerprint != repriced.fingerprint


def test_matrix_rejects_unregistered_or_out_of_range_values(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.toml"
    invalid.write_text(
        'schema_version=1\n[parameters]\n"agents.max_depth"=[2]\n[subsystems]\nagents=["agents.max_depth"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="registered range"):
        load_matrix(invalid)


def test_matrix_filters_cross_parameter_incompatible_candidates(tmp_path: Path) -> None:
    invalid = tmp_path / "cross-parameter.toml"
    invalid.write_text(
        "schema_version=1\nmatrix_id='cross'\ntask_set_version='cross'\n"
        '[parameters]\n"context.trigger_ratio"=[0.70]\n'
        '"context.target_ratio"=[0.80]\n"agents.max_children"=[2]\n'
        '"agents.max_concurrency"=[4]\n[subsystems]\n'
        'context=["context.trigger_ratio", "context.target_ratio"]\n'
        'agents=["agents.max_children", "agents.max_concurrency"]\n',
        encoding="utf-8",
    )
    matrix = load_matrix(invalid)
    candidates = matrix.candidates(strategy="subsystem")
    assert len(candidates) == 1
    assert candidates[0].name == "baseline"


def test_simulator_exposes_safety_and_agent_conflict_metrics() -> None:
    candidate = PolicyCandidate("baseline", baseline_values())
    task = build_tasks(split="confirm")[-1]
    conflict = type(task)(
        task.id,
        task.subsystem,
        task.split,
        task.prompt,
        {
            **task.requirements,
            "conflicting": True,
            "force_unresolved_conflict": True,
        },
        task.critical,
    )
    _, _, _, _, metrics = _simulate(conflict, candidate)
    assert metrics["unresolved_agent_conflict"] == 1
    assert metrics["agent_conflict_cases"] == 1


@pytest.mark.parametrize(
    ("requirement", "metric"),
    [
        ("unauthorized_action", "unauthorized_action"),
        ("approval_bypass", "approval_bypass"),
        ("duplicate_destructive_side_effect", "duplicate_destructive_side_effect"),
        ("cross_workspace_leak", "cross_workspace_leak"),
        ("unknown_execution_auto_replay", "unknown_execution_auto_replay"),
    ],
)
def test_each_governance_fixture_is_counted_and_gated(
    requirement: str, metric: str
) -> None:
    candidate = PolicyCandidate("baseline", baseline_values())
    base = build_tasks(split="confirm")[0]
    task = type(base)(
        base.id,
        base.subsystem,
        base.split,
        base.prompt,
        {**base.requirements, requirement: True},
        base.critical,
    )
    success, _, _, _, metrics = _simulate(task, candidate)
    assert success is True
    assert metrics[metric] == 1
    result = analyse_candidates(
        [candidate],
        [
            SampleResult(
                task.id,
                task.subsystem,
                candidate.name,
                candidate.fingerprint,
                0,
                1,
                True,
                None,
                0.1,
                1,
                1,
                1,
                0.0,
                metrics,
            )
        ],
        stage="deterministic",
    )
    summary = result["candidates"][0]
    assert metric in metrics
    gate = {
        "unauthorized_action": "unauthorized_actions",
        "approval_bypass": "approval_bypasses",
        "duplicate_destructive_side_effect": "duplicate_destructive_side_effects",
        "cross_workspace_leak": "cross_workspace_leaks",
        "unknown_execution_auto_replay": "unknown_execution_auto_replays",
    }[requirement]
    assert gate in summary["gate_failures"]


def test_selection_rejects_samples_with_security_violations() -> None:
    candidate = PolicyCandidate("baseline", baseline_values())
    sample = SampleResult(
        "security-case",
        "runtime",
        candidate.name,
        candidate.fingerprint,
        0,
        1,
        True,
        None,
        0.1,
        1,
        1,
        1,
        0.0,
        {"unauthorized_action": 1},
    )
    result = analyse_candidates([candidate], [sample], stage="deterministic")
    summary = result["candidates"][0]
    assert "unauthorized_actions" in summary["gate_failures"]
    assert summary["feasible"] is False


def test_selection_separates_quality_capacity_and_safe_stop_metrics() -> None:
    candidate = PolicyCandidate("baseline", baseline_values())

    def result(
        task_id: str,
        success: bool,
        *,
        in_budget: bool,
        capacity_case: bool = False,
        capacity_covered: bool = False,
        safe_stop_case: bool = False,
        safe_stop_correct: bool = False,
    ) -> SampleResult:
        return SampleResult(
            task_id,
            "runtime",
            candidate.name,
            candidate.fingerprint,
            0,
            1,
            success,
            None,
            0.1,
            1,
            1,
            1,
            0.0,
            {
                "in_budget": int(in_budget),
                "capacity_case": int(capacity_case),
                "capacity_covered": int(capacity_covered),
                "safe_stop_case": int(safe_stop_case),
                "safe_stop_correct": int(safe_stop_correct),
            },
        )

    samples = [
        result("quality-pass", True, in_budget=True),
        result(
            "quality-stop",
            False,
            in_budget=True,
            safe_stop_case=True,
            safe_stop_correct=True,
        ),
        result(
            "capacity-covered",
            True,
            in_budget=False,
            capacity_case=True,
            capacity_covered=True,
        ),
        result(
            "capacity-stop",
            False,
            in_budget=False,
            capacity_case=True,
            safe_stop_case=True,
            safe_stop_correct=True,
        ),
    ]
    summary = analyse_candidates([candidate], samples, stage="deterministic")[
        "candidates"
    ][0]
    # The primary rate excludes capacity-pressure cases.  The raw rate is
    # retained separately so a covered capacity case does not inflate quality.
    assert summary["success_rate"] == pytest.approx(0.5)
    assert summary["raw_success_rate"] == pytest.approx(0.5)
    assert summary["quality_success_rate"] == pytest.approx(0.5)
    assert summary["quality_by_subsystem"]["runtime"]["success_rate"] == pytest.approx(
        0.5
    )
    assert summary["capacity_coverage"] == pytest.approx(0.5)
    assert summary["safe_stop_rate"] == pytest.approx(1.0)


def test_success_rate_excludes_capacity_even_when_capacity_case_is_mislabelled() -> (
    None
):
    candidate = PolicyCandidate("baseline", baseline_values())

    def sample(task_id: str, success: bool, **metrics: int) -> SampleResult:
        return SampleResult(
            task_id,
            "runtime",
            candidate.name,
            candidate.fingerprint,
            0,
            1,
            success,
            None,
            0.1,
            1,
            1,
            1,
            0.0,
            metrics,
        )

    summary = analyse_candidates(
        [candidate],
        [
            sample("normal", True, in_budget=1),
            # A malformed legacy sample must not enter the quality denominator.
            sample("capacity", True, in_budget=1, capacity_case=1),
            sample("normal-fail", False, in_budget=1),
        ],
        stage="deterministic",
    )["candidates"][0]
    assert summary["success_rate"] == pytest.approx(0.5)
    assert summary["raw_success_rate"] == pytest.approx(2 / 3)
    assert summary["quality_samples"] == 2


def test_core_dataset_labels_quality_and_capacity_cases() -> None:
    tasks = build_tasks(split="tune")
    assert all("in_budget" in task.requirements for task in tasks)
    assert sum(bool(task.requirements["in_budget"]) for task in tasks) == 167
    assert sum(bool(task.requirements["capacity_case"]) for task in tasks) == 53


def test_refine_keeps_two_levels_and_bounds_subsystem_products(tmp_path: Path) -> None:
    matrix = load_matrix(MATRIX)
    summaries = [
        {
            "name": candidate.name,
            "values": candidate.values,
            "feasible": True,
            "success_rate": 1.0,
            "median_cost_usd": index,
            "latency_seconds": {"p95": index},
        }
        for index, candidate in enumerate(matrix.candidates())
    ]
    path = tmp_path / "oat.json"
    path.write_text(
        json.dumps(
            {
                "manifest": {"stage": "deterministic"},
                "summary": {"candidates": summaries},
            }
        ),
        encoding="utf-8",
    )
    refined = _policy_script()._refined_candidates(path, matrix)
    assert 2 < len(refined) <= 89
    assert len({item.fingerprint for item in refined}) == len(refined)


def test_confirmation_requires_distinct_seed_agreement(tmp_path: Path) -> None:
    module = _policy_script()
    first = {
        "manifest": {"stage": "confirm", "seed": 1},
        "summary": {
            "recommendation": {
                "candidate_fingerprint": "winner",
                "action": "request_human_approval",
                "risks": [],
            }
        },
        "report_hash": "old",
    }
    module._finalize_confirmation(first, None)
    assert first["summary"]["recommendation"]["action"] == (
        "requires_second_confirmation"
    )
    peer = tmp_path / "peer.json"
    peer.write_text(json.dumps(first), encoding="utf-8")
    second = {
        "manifest": {"stage": "confirm", "seed": 2},
        "summary": {
            "recommendation": {
                "candidate_fingerprint": "winner",
                "action": "request_human_approval",
                "risks": [],
            }
        },
        "report_hash": "old-2",
    }
    module._finalize_confirmation(second, peer)
    assert second["summary"]["recommendation"]["action"] == ("request_human_approval")
    second["manifest"]["seed"] = 1
    with pytest.raises(ValueError, match="different seeds"):
        module._finalize_confirmation(second, peer)


def test_statistics_are_deterministic_and_paired() -> None:
    assert percentile([0, 10], 0.95) == pytest.approx(9.5)
    lower, upper = wilson_interval(100, 100)
    assert 0.96 < lower < upper <= 1
    first = paired_bootstrap_interval(
        [True, True, False], [True, False, False], samples=500
    )
    second = paired_bootstrap_interval(
        [True, True, False], [True, False, False], samples=500
    )
    assert first == second
    with pytest.raises(ValueError, match="equal length"):
        paired_bootstrap_interval([True], [])


def test_memory_weight_optimizer_is_seeded_and_stays_on_simplex() -> None:
    base = PolicyCandidate("baseline", baseline_values())
    first = propose_memory_weights(base, [], count=8, seed=7)
    second = propose_memory_weights(base, [], count=8, seed=7)
    assert [item.fingerprint for item in first] == [item.fingerprint for item in second]
    for item in first:
        assert sum(
            float(item.values[path])
            for path in (
                "memory.retrieval_weight",
                "memory.scope_weight",
                "memory.confidence_weight",
                "memory.freshness_weight",
                "memory.source_validity_weight",
            )
        ) == pytest.approx(1.0)


def _sample(
    candidate: PolicyCandidate, task: str, success: bool, *, cost: float, latency: float
) -> SampleResult:
    return SampleResult(
        task,
        "runtime",
        candidate.name,
        candidate.fingerprint,
        0,
        1,
        success,
        None,
        latency,
        1,
        10,
        2,
        cost,
    )


def test_selection_uses_noninferiority_and_pareto_rules() -> None:
    values = baseline_values()
    baseline = PolicyCandidate("baseline", values)
    faster = PolicyCandidate("faster", {**values, "tools.max_read_concurrency": 8})
    samples = [
        _sample(baseline, f"task-{index}", True, cost=1, latency=2)
        for index in range(20)
    ] + [
        _sample(faster, f"task-{index}", True, cost=0.8, latency=1)
        for index in range(20)
    ]
    result = analyse_candidates([baseline, faster], samples, stage="deterministic")
    assert faster.fingerprint in result["pareto_fingerprints"]
    assert result["recommendation"]["candidate"] == "faster"
    assert result["recommendation"]["action"] == "request_human_approval"


def test_runner_writes_complete_reproducible_artifacts(tmp_path: Path) -> None:
    matrix = load_matrix(MATRIX)
    candidates = matrix.candidates()[:2]
    tasks = build_tasks(split="tune")[:5]
    runner = EvaluationRunner(matrix, stage="deterministic", seed=7, repetitions=1)
    report, samples = asyncio.run(runner.run(tasks, candidates))
    repeated, _ = asyncio.run(
        EvaluationRunner(matrix, stage="deterministic", seed=7, repetitions=1).run(
            tasks, candidates
        )
    )
    assert repeated["report_hash"] == report["report_hash"]
    output = tmp_path / "result"
    write_evaluation(output, report, samples)
    assert {item.name for item in output.iterdir()} >= {
        "report.json",
        "samples.jsonl",
        "pareto.svg",
        "reproduce.txt",
    }
    loaded = json.loads((output / "report.json").read_text())
    schema = json.loads((ROOT / "evaluations" / "report-schema-v1.json").read_text())
    jsonschema.validate(loaded, schema)
    assert loaded["manifest"]["seed"] == 7
    assert loaded["manifest"]["task_set_hash"]
    assert loaded["report_hash"] == report["report_hash"]


def test_live_stage_requires_explicit_identity_and_records_provider_failure() -> None:
    matrix = load_matrix(MATRIX)
    with pytest.raises(ValueError, match="explicit provider and model"):
        EvaluationRunner(matrix, stage="screen", seed=1, repetitions=1)

    async def failed_probe(task, provider, model):
        raise TimeoutError("simulated timeout")

    runner = EvaluationRunner(
        matrix,
        stage="screen",
        seed=1,
        repetitions=1,
        provider="test",
        model="test-model",
        live_probe=failed_probe,
    )
    report, samples = asyncio.run(
        runner.run(build_tasks(split="tune")[:1], matrix.candidates()[:1])
    )
    assert report["status"] == "completed"
    assert samples[0].stop_reason == "provider_error"
    assert "TimeoutError" in str(samples[0].error)


def test_candidate_probe_receives_policy_and_uses_candidate_cache_key() -> None:
    matrix = load_matrix(MATRIX)
    baseline, faster = matrix.candidates()[:2]
    seen: list[str] = []

    async def probe(task, candidate, provider, model):
        seen.append(candidate.fingerprint)
        return True, 3, 2

    runner = EvaluationRunner(
        matrix,
        stage="screen",
        seed=1,
        repetitions=1,
        provider="test",
        model="test-model",
        candidate_probe=probe,
    )
    report, _ = asyncio.run(
        runner.run(build_tasks(split="confirm")[:1], [baseline, faster])
    )
    assert report["manifest"]["execution_mode"] == "candidate_probe"
    assert seen == [baseline.fingerprint, faster.fingerprint]


def test_candidate_probe_outcome_is_authoritative_over_synthetic_capacity_result() -> (
    None
):
    matrix = load_matrix(MATRIX)
    candidate = matrix.candidates()[0]
    # This runtime task is outside the baseline capacity, so _simulate() fails
    # it while the injected real-runtime probe is declared successful.
    task = build_tasks(split="tune")[5]
    assert task.subsystem == "runtime"
    assert task.requirements["in_budget"] is False

    async def probe(task, selected, provider, model):
        return {"success": True, "input_tokens": 2, "output_tokens": 1}

    runner = EvaluationRunner(
        matrix,
        stage="screen",
        seed=1,
        repetitions=1,
        provider="test",
        model="test-model",
        candidate_probe=probe,
    )
    _, samples = asyncio.run(runner.run((task,), [candidate]))
    assert samples[0].success is True
    # A candidate-aware probe is authoritative; the synthetic capacity stop
    # reason must not survive a successful real-runtime result.
    assert samples[0].stop_reason is None


def test_candidate_probe_explicitly_clears_synthetic_stop_reason() -> None:
    matrix = load_matrix(MATRIX)
    candidate = matrix.candidates()[0]
    task = build_tasks(split="tune")[5]

    async def probe(task, selected, provider, model):
        return {
            "success": True,
            "stop_reason": None,
            "input_tokens": 1,
            "output_tokens": 1,
        }

    runner = EvaluationRunner(
        matrix,
        stage="screen",
        seed=1,
        repetitions=1,
        provider="test",
        model="test-model",
        candidate_probe=probe,
    )
    _, samples = asyncio.run(runner.run((task,), [candidate]))
    assert samples[0].success is True
    assert samples[0].stop_reason is None


def test_candidate_probe_cannot_change_reviewed_workload_labels() -> None:
    matrix = load_matrix(MATRIX)
    candidate = matrix.candidates()[0]
    task = build_tasks(split="tune")[0]
    assert task.requirements["in_budget"] is True

    async def probe(task, selected, provider, model):
        return {
            "success": True,
            "input_tokens": 1,
            "output_tokens": 1,
            "metrics": {"in_budget": 0, "capacity_case": 1, "approval_bypass": 1},
        }

    runner = EvaluationRunner(
        matrix,
        stage="screen",
        seed=1,
        repetitions=1,
        provider="test",
        model="test-model",
        candidate_probe=probe,
    )
    report, _ = asyncio.run(runner.run((task,), [candidate]))
    summary = report["summary"]["candidates"][0]
    assert summary["quality_samples"] == 1
    assert summary["capacity_cases"] == 0
    assert summary["approval_bypasses"] == 1


def test_model_only_live_probe_cannot_be_used_for_policy_recommendation() -> None:
    matrix = load_matrix(MATRIX)

    async def probe(task, provider, model):
        return True, 3, 2

    runner = EvaluationRunner(
        matrix,
        stage="screen",
        seed=1,
        repetitions=1,
        provider="test",
        model="test-model",
        live_probe=probe,
    )
    report, _ = asyncio.run(
        runner.run(build_tasks(split="confirm"), matrix.candidates()[:1])
    )
    summary = report["summary"]["candidates"][0]
    assert "candidate_policy_injection" in summary["gate_failures"]
    assert report["summary"]["recommendation"] is None


def test_rich_candidate_probe_fields_are_preserved_in_sample() -> None:
    matrix = load_matrix(MATRIX)
    candidate = matrix.candidates()[0]

    async def probe(task, selected, provider, model):
        assert selected.fingerprint == candidate.fingerprint
        return {
            "success": True,
            "input_tokens": 11,
            "output_tokens": 7,
            "stop_reason": "completed",
            "tool_calls": 2,
            "compaction_events": 1,
            "approval_events": 1,
            "conflicts": 0,
            "metrics": {"approval_bypass": 0},
        }

    runner = EvaluationRunner(
        matrix,
        stage="screen",
        seed=1,
        repetitions=1,
        provider="test",
        model="test-model",
        candidate_probe=probe,
    )
    task = build_tasks(split="confirm")[0]
    _, samples = asyncio.run(runner.run((task,), [candidate]))
    sample = samples[0]
    assert sample.input_tokens == max(8, len(task.prompt.encode()) // 3) + 11
    assert sample.output_tokens == 15
    assert sample.stop_reason == "completed"
    assert (sample.tool_calls, sample.compaction_events) == (2, 1)
    assert (sample.approval_events, sample.conflicts) == (1, 0)
    assert sample.metrics["stop_reason"] == "completed"


def test_reasoning_probe_options_are_provider_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _reasoning_request_options("capslock", "deepseek-v4-flash") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert _reasoning_request_options("openai", "gpt-4.1-mini") == {}
    monkeypatch.setenv("TEST_DISABLE_THINKING", "1")
    assert _reasoning_request_options("test", "custom-model") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }


def test_answer_matching_handles_formatting_but_rejects_ambiguity() -> None:
    assert _answer_matches("`CAPSLOCK_EVAL_OK`", "CAPSLOCK_EVAL_OK")
    assert _answer_matches("Done: CAPSLOCK_EVAL_OK.", "CAPSLOCK_EVAL_OK")
    assert not _answer_matches("STOP then CONTINUE", "CONTINUE")
    assert not _answer_matches("No answer", "CAPSLOCK_EVAL_OK")


def test_runtime_probe_maps_every_candidate_policy_surface(tmp_path: Path) -> None:
    settings = Settings.load(tmp_path)
    base = baseline_values()
    candidate = PolicyCandidate(
        "probe-policy",
        {
            **base,
            "runtime.max_tool_rounds": 48,
            "providers.timeout_seconds": 90,
            "tools.max_read_concurrency": 8,
            "tools.max_argument_repair_attempts": 2,
            "context.trigger_ratio": 0.85,
            "context.target_ratio": 0.5,
            "context.preserve_recent_turns": 8,
            "context.preserve_recent_tokens": 49152,
            "context.max_compaction_failures": 4,
            "loop_detection.max_cycle_length": 6,
            "memory.recall_limit": 8,
            "memory.recall_bytes": 8192,
            "memory.semantic_threshold": 0.35,
            "memory.recall_threshold": 0.4,
            "agents.max_children": 8,
            "agents.max_concurrency": 4,
            "agents.max_child_tool_rounds": 24,
        },
    )
    updated, memory_policy = _settings_for_candidate(
        settings, candidate, provider="capslock", model="probe-model"
    )
    assert updated.runtime.max_tool_rounds == 48
    assert updated.tools.max_read_concurrency == 8
    assert updated.tools.max_argument_repair_attempts == 2
    assert (updated.context.trigger_ratio, updated.context.target_ratio) == (0.85, 0.5)
    assert updated.context.preserve_recent_tokens == 49152
    assert updated.loop_detection.max_cycle_length == 6
    assert (updated.agents.max_children, updated.agents.max_concurrency) == (8, 4)
    assert updated.agents.max_child_tool_rounds == 24
    assert (
        memory_policy.limit,
        memory_policy.byte_budget,
        memory_policy.semantic_threshold,
        memory_policy.recall_threshold,
    ) == (8, 8192, 0.35, 0.4)


def test_live_provider_errors_cannot_produce_a_recommendation() -> None:
    candidate = PolicyCandidate("baseline", baseline_values())
    sample = SampleResult(
        "provider-case",
        "runtime",
        candidate.name,
        candidate.fingerprint,
        0,
        1,
        False,
        "provider_error",
        0.1,
        1,
        1,
        1,
        0.0,
    )
    result = analyse_candidates([candidate], [sample], stage="screen")
    summary = result["candidates"][0]
    assert summary["provider_error_rate"] == pytest.approx(1.0)
    assert "provider_health" in summary["gate_failures"]
    assert result["recommendation"] is None


def test_live_candidate_requires_minimum_quality_sample_count() -> None:
    candidate = PolicyCandidate("baseline", baseline_values())
    sample = SampleResult(
        "quality-case",
        "runtime",
        candidate.name,
        candidate.fingerprint,
        0,
        1,
        True,
        None,
        0.1,
        1,
        1,
        1,
        0.0,
    )
    result = analyse_candidates([candidate], [sample], stage="confirm")
    summary = result["candidates"][0]
    assert summary["quality_power_ok"] is False
    assert "insufficient_power" in summary["gate_failures"]
    assert result["recommendation"] is None


def test_live_probe_is_reused_across_policy_candidates() -> None:
    matrix = load_matrix(MATRIX)
    calls = 0

    async def probe(task, provider, model):
        nonlocal calls
        calls += 1
        return True, 10, 2

    runner = EvaluationRunner(
        matrix,
        stage="screen",
        seed=1,
        repetitions=1,
        provider="test",
        model="test-model",
        live_probe=probe,
    )
    asyncio.run(runner.run(build_tasks(split="tune")[:1], matrix.candidates()[:3]))
    assert calls == 1
