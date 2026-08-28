from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from capslock.evaluation import (
    EvaluationRunner,
    build_tasks,
    load_matrix,
    propose_memory_weights,
)
from capslock.evaluation.models import PolicyCandidate, SampleResult
from capslock.evaluation.registry import baseline_values, registry_document
from capslock.evaluation.runner import write_evaluation
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


def test_matrix_rejects_unregistered_or_out_of_range_values(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.toml"
    invalid.write_text(
        'schema_version=1\n[parameters]\n"agents.max_depth"=[2]\n[subsystems]\nagents=["agents.max_depth"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="registered range"):
        load_matrix(invalid)


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
