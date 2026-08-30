#!/usr/bin/env python3
"""Run the versioned CapsLock behavioural-policy evaluation funnel."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capslock.evaluation import (  # noqa: E402
    EvaluationRunner,
    build_tasks,
    load_matrix,
    propose_memory_weights,
)
from capslock.evaluation.models import PolicyCandidate, canonical_hash  # noqa: E402
from capslock.evaluation.registry import baseline_values  # noqa: E402
from capslock.evaluation.runner import write_evaluation  # noqa: E402


def _load_candidate_probe(spec: str | None):
    """Load an async candidate-aware probe as ``module:callable``."""
    if not spec:
        return None
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("--candidate-probe must use module:callable syntax")
    module = importlib.import_module(module_name)
    probe = getattr(module, attribute, None)
    if not callable(probe):
        raise ValueError(f"candidate probe is not callable: {spec}")
    return probe


def _candidates_from_report(path: Path) -> list[PolicyCandidate]:
    report = json.loads(path.read_text(encoding="utf-8"))
    summaries = report.get("summary", {}).get("candidates", [])
    pareto = set(report.get("summary", {}).get("pareto_fingerprints", []))
    baseline = next(
        (item for item in summaries if item.get("name") == "baseline"), None
    )
    ranked = sorted(
        (item for item in summaries if item.get("fingerprint") in pareto),
        key=lambda item: (
            -float(item.get("quality_success_rate", item["success_rate"])),
            float(item["median_cost_usd"]),
            float(item["latency_seconds"]["p95"]),
        ),
    )
    limit = 8 if report.get("manifest", {}).get("stage") == "deterministic" else 3
    chosen = ([baseline] if baseline else []) + ranked[:limit]
    unique: dict[str, PolicyCandidate] = {}
    for item in chosen:
        if item is not None:
            candidate = PolicyCandidate(
                str(item["name"]), {**baseline_values(), **dict(item["values"])}
            )
            unique[candidate.fingerprint] = candidate
    if not unique:
        raise ValueError("input report has no usable baseline or Pareto candidates")
    return list(unique.values())


def _refined_candidates(path: Path, matrix) -> list[PolicyCandidate]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("manifest", {}).get("stage") != "deterministic":
        raise ValueError("refine requires a deterministic OAT report")
    summaries = report.get("summary", {}).get("candidates", [])
    baseline = baseline_values()
    levels: dict[str, tuple[int | float, ...]] = {}
    for parameter in matrix.parameters:
        matching = []
        for item in summaries:
            values = item.get("values", {})
            differences = {
                name for name, value in values.items() if baseline.get(name) != value
            }
            if not differences or differences == {parameter}:
                matching.append(item)
        matching.sort(
            key=lambda item: (
                not bool(item.get("feasible")),
                -float(item.get("quality_success_rate", item.get("success_rate", 0))),
                float(item.get("median_cost_usd", 0)),
                float(item.get("latency_seconds", {}).get("p95", 0)),
            )
        )
        selected: list[int | float] = []
        for item in matching:
            value = item["values"][parameter]
            if value not in selected:
                selected.append(value)
            if len(selected) == 2:
                break
        if len(selected) < 2:
            raise ValueError(f"OAT report has fewer than two levels for {parameter}")
        levels[parameter] = tuple(selected)
    candidates = [PolicyCandidate("baseline", baseline)]
    seen = {candidates[0].fingerprint}
    for subsystem, parameters in matrix.subsystems.items():
        for combination in itertools.product(*(levels[name] for name in parameters)):
            overrides = dict(zip(parameters, combination, strict=True))
            candidate = PolicyCandidate(
                subsystem
                + ":"
                + ",".join(f"{name}={value}" for name, value in overrides.items()),
                {**baseline, **overrides},
            )
            if candidate.fingerprint not in seen:
                seen.add(candidate.fingerprint)
                candidates.append(candidate)
    return candidates


def _memory_weight_candidates(path: Path, *, seed: int) -> list[PolicyCandidate]:
    report = json.loads(path.read_text(encoding="utf-8"))
    summaries = report.get("summary", {}).get("candidates", [])
    shortlisted = [
        item
        for item in summaries
        if item.get("feasible")
        and (
            item.get("name") == "baseline"
            or str(item.get("name", "")).startswith("memory")
        )
    ]
    shortlisted.sort(
        key=lambda item: (
            -float(item.get("quality_success_rate", item.get("success_rate", 0))),
            float(item.get("median_cost_usd", 0)),
            float(item.get("latency_seconds", {}).get("p95", 0)),
        )
    )
    if not shortlisted:
        raise ValueError("input report has no feasible Memory candidates")
    candidates = [PolicyCandidate("baseline", baseline_values())]
    seen = {candidates[0].fingerprint}
    pareto = set(report.get("summary", {}).get("pareto_fingerprints", []))
    global_shortlist = sorted(
        (
            item
            for item in summaries
            if item.get("feasible") and item.get("fingerprint") in pareto
        ),
        key=lambda item: (
            -float(item.get("quality_success_rate", item.get("success_rate", 0))),
            float(item.get("median_cost_usd", 0)),
            float(item.get("latency_seconds", {}).get("p95", 0)),
        ),
    )
    for item in global_shortlist[:8]:
        candidate = PolicyCandidate(
            str(item["name"]), {**baseline_values(), **dict(item["values"])}
        )
        if candidate.fingerprint not in seen:
            seen.add(candidate.fingerprint)
            candidates.append(candidate)
    for index, item in enumerate(shortlisted[:3]):
        base = PolicyCandidate(
            str(item["name"]), {**baseline_values(), **dict(item["values"])}
        )
        for proposal in propose_memory_weights(
            base, summaries, count=16, seed=seed + index
        ):
            if proposal.fingerprint not in seen:
                seen.add(proposal.fingerprint)
                candidates.append(proposal)
    return candidates


def _finalize_confirmation(report: dict, peer_path: Path | None) -> None:
    recommendation = report.get("summary", {}).get("recommendation")
    if recommendation is None:
        return
    if peer_path is None:
        recommendation["action"] = "requires_second_confirmation"
    else:
        peer = json.loads(peer_path.read_text(encoding="utf-8"))
        if peer.get("manifest", {}).get("stage") != "confirm":
            raise ValueError("--peer-report must be a confirm report")
        if peer.get("manifest", {}).get("seed") == report["manifest"]["seed"]:
            raise ValueError("confirm reports must use different seeds")
        peer_recommendation = peer.get("summary", {}).get("recommendation") or {}
        agreed = peer_recommendation.get("candidate_fingerprint") == recommendation.get(
            "candidate_fingerprint"
        ) and peer_recommendation.get("action") in {
            "requires_second_confirmation",
            "request_human_approval",
        }
        recommendation["action"] = (
            "request_human_approval" if agreed else "keep_current_defaults"
        )
        if not agreed:
            recommendation["risks"] = [
                *recommendation.get("risks", []),
                "confirmation_disagreement",
            ]
        recommendation["peer_report_hash"] = peer.get("report_hash")
    _rehash(report)


def _set_stage_action(report: dict, action: str) -> None:
    recommendation = report.get("summary", {}).get("recommendation")
    if recommendation is None:
        return
    recommendation["action"] = action
    _rehash(report)


def _rehash(report: dict) -> None:
    recommendation = report.get("summary", {}).get("recommendation")
    if recommendation is not None:
        recommendation.pop("recommendation_hash", None)
        recommendation["recommendation_hash"] = canonical_hash(recommendation)
    report.pop("report_hash", None)
    report["report_hash"] = canonical_hash(report)


async def async_main(args: argparse.Namespace) -> int:
    matrix = load_matrix(args.matrix)
    if args.stage == "deterministic":
        tasks = build_tasks(split="tune")
        if args.strategy == "refine":
            if args.input_report is None:
                raise ValueError("refine requires --input-report from the OAT stage")
            candidates = _refined_candidates(args.input_report, matrix)
        elif args.strategy == "memory-optimize":
            if args.input_report is None:
                raise ValueError("memory-optimize requires --input-report")
            candidates = _memory_weight_candidates(args.input_report, seed=args.seed)
        else:
            candidates = matrix.candidates(strategy="oat")
    else:
        if args.input_report is None:
            raise ValueError("screen and confirm require --input-report")
        tasks = build_tasks(split="confirm" if args.stage == "confirm" else "tune")
        candidates = _candidates_from_report(args.input_report)
    runner = EvaluationRunner(
        matrix,
        stage=args.stage,
        seed=args.seed,
        repetitions=args.repetitions,
        provider=args.provider,
        model=args.model,
        candidate_probe=_load_candidate_probe(args.candidate_probe),
    )
    report, samples = await runner.run(tasks, candidates)
    if args.stage == "deterministic":
        _set_stage_action(report, "requires_live_screen")
    elif args.stage == "screen":
        _set_stage_action(report, "requires_target_confirmation")
    else:
        _finalize_confirmation(report, args.peer_report)
    write_evaluation(args.output, report, samples)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report_hash": report["report_hash"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["summary"]["recommendation"] is not None else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage", choices=("deterministic", "screen", "confirm"), required=True
    )
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument(
        "--candidate-probe",
        help="async module:callable receiving (task, candidate, provider, model)",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument(
        "--strategy",
        choices=("oat", "refine", "memory-optimize"),
        default="oat",
    )
    parser.add_argument("--input-report", type=Path)
    parser.add_argument("--peer-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.stage != "deterministic" and (not args.provider or not args.model):
        parser.error("screen and confirm require explicit --provider and --model")
    try:
        return asyncio.run(async_main(args))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
