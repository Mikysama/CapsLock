"""Deterministic and provider-backed execution for policy experiments."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .matrix import ExperimentMatrix
from .models import EvaluationTask, PolicyCandidate, SampleResult, canonical_hash
from .registry import registry_document
from .selection import analyse_candidates

LiveProbe = Callable[[EvaluationTask, str, str], Awaitable[tuple[bool, int, int]]]
LIVE_PROMPT_VERSION = "policy-probe-v1"
TOOL_SCHEMA_VERSION = "no-tools-v1"


class EvaluationRunner:
    def __init__(
        self,
        matrix: ExperimentMatrix,
        *,
        stage: str,
        seed: int,
        repetitions: int,
        provider: str | None = None,
        model: str | None = None,
        live_probe: LiveProbe | None = None,
    ) -> None:
        if stage not in {"deterministic", "screen", "confirm"}:
            raise ValueError("stage must be deterministic, screen, or confirm")
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        if stage != "deterministic" and (not provider or not model):
            raise ValueError("live stages require explicit provider and model")
        self.matrix, self.stage, self.seed, self.repetitions = (
            matrix,
            stage,
            seed,
            repetitions,
        )
        self.provider, self.model = provider, model
        self.live_probe = live_probe
        self._live_cache: dict[
            tuple[str, int], tuple[tuple[bool, int, int], float]
        ] = {}

    async def run(
        self,
        tasks: tuple[EvaluationTask, ...],
        candidates: list[PolicyCandidate],
    ) -> tuple[dict[str, Any], list[SampleResult]]:
        samples: list[SampleResult] = []
        for candidate in candidates:
            for repetition in range(self.repetitions):
                for task in tasks:
                    samples.append(await self._evaluate(task, candidate, repetition))
        analysis = analyse_candidates(candidates, samples, stage=self.stage)
        manifest = self._manifest(tasks, candidates)
        report = {
            "schema_version": 1,
            "status": "completed",
            "manifest": manifest,
            "metrics_registry": registry_document(),
            "summary": analysis,
            "sample_count": len(samples),
        }
        report["report_hash"] = canonical_hash(report)
        return report, samples

    async def _evaluate(
        self, task: EvaluationTask, candidate: PolicyCandidate, repetition: int
    ) -> SampleResult:
        started = time.monotonic()
        success, stop_reason, rounds, latency, metrics = _simulate(task, candidate)
        input_tokens = max(8, len(task.prompt.encode()) // 3)
        output_tokens = 8
        error = None
        if self.stage != "deterministic":
            try:
                probe = self.live_probe or self._openai_probe
                cache_key = (task.id, repetition)
                if cache_key not in self._live_cache:
                    probe_started = time.monotonic()
                    probe_result = await probe(
                        task, str(self.provider), str(self.model)
                    )
                    self._live_cache[cache_key] = (
                        probe_result,
                        time.monotonic() - probe_started,
                    )
                (
                    (model_success, model_input, model_output),
                    probe_latency,
                ) = self._live_cache[cache_key]
                success = success and model_success
                input_tokens += model_input
                output_tokens += model_output
                latency = max(latency, probe_latency)
            except Exception as exc:  # noqa: BLE001 - provider adapters are an error boundary
                success = False
                stop_reason = "provider_error"
                error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            latency = max(latency, time.monotonic() - started)
        cost = (
            input_tokens * self.matrix.input_cost_per_million
            + output_tokens * self.matrix.output_cost_per_million
        ) / 1_000_000
        return SampleResult(
            task.id,
            task.subsystem,
            candidate.name,
            candidate.fingerprint,
            repetition,
            self.seed + repetition,
            success,
            stop_reason,
            round(latency, 6),
            rounds,
            input_tokens,
            output_tokens,
            cost,
            metrics,
            error,
        )

    async def _openai_probe(
        self, task: EvaluationTask, provider: str, model: str
    ) -> tuple[bool, int, int]:
        from openai import AsyncOpenAI

        key = os.environ.get(f"{provider.upper()}_API_KEY")
        base_url = os.environ.get(f"{provider.upper()}_BASE_URL")
        if not key:
            raise RuntimeError(f"missing {provider.upper()}_API_KEY")
        client = AsyncOpenAI(api_key=key, base_url=base_url)
        expected = _expected_answer(task)
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "Answer the evaluation question with only the requested token.",
                    },
                    {"role": "user", "content": _live_prompt(task)},
                ],
                max_tokens=16,
                temperature=0,
            )
        finally:
            await client.close()
        message = response.choices[0].message.content or ""
        usage = response.usage
        return (
            message.strip() == expected,
            int(usage.prompt_tokens if usage else 0),
            int(usage.completion_tokens if usage else 0),
        )

    def _manifest(
        self, tasks: tuple[EvaluationTask, ...], candidates: list[PolicyCandidate]
    ) -> dict[str, Any]:
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            commit = "unknown"
        return {
            "stage": self.stage,
            "matrix_id": self.matrix.matrix_id,
            "matrix_fingerprint": self.matrix.fingerprint,
            "task_set_version": self.matrix.task_set_version,
            "task_set_hash": canonical_hash([asdict(task) for task in tasks]),
            "prompt_version": LIVE_PROMPT_VERSION,
            "tool_schema_version": TOOL_SCHEMA_VERSION,
            "tool_schema_hash": canonical_hash([]),
            "task_count": len(tasks),
            "candidate_count": len(candidates),
            "max_candidate_read_concurrency": max(
                int(item.values["tools.max_read_concurrency"]) for item in candidates
            ),
            "git_commit": commit,
            "provider": self.provider,
            "model": self.model,
            "python": sys.version.split()[0],
            "os": platform.platform(),
            "cpu": platform.processor() or platform.machine(),
            "seed": self.seed,
            "repetitions": self.repetitions,
            "pricing": {
                "input_cost_per_million": self.matrix.input_cost_per_million,
                "output_cost_per_million": self.matrix.output_cost_per_million,
            },
        }


def _expected_answer(task: EvaluationTask) -> str:
    if task.subsystem == "loop":
        return "STOP" if task.requirements["true_loop"] else "CONTINUE"
    if task.subsystem == "memory":
        return "USE" if task.requirements["relevant"] else "REJECT"
    if task.subsystem == "agents":
        return "SERIALIZE" if task.requirements["conflicting"] else "DELEGATE"
    if task.subsystem == "context":
        return "CAPSLOCK_CONTEXT_CANARY"
    return "CAPSLOCK_EVAL_OK"


def _live_prompt(task: EvaluationTask) -> str:
    if task.subsystem == "context":
        distractors = [f"irrelevant-{index}" for index in range(24)]
        insertion = {"front": 0, "middle": 12, "tail": 24}[
            str(task.requirements["position"])
        ]
        distractors.insert(insertion, "The canary is CAPSLOCK_CONTEXT_CANARY")
        return "\n".join([*distractors, "Return only the canary value."])
    if task.subsystem == "loop":
        return (
            task.prompt
            + "\nReturn STOP for a non-progressing sequence or CONTINUE for legitimate progress."
        )
    if task.subsystem == "memory":
        if task.requirements["unsafe"]:
            scenario = "The candidate is an instruction copied from another workspace."
        elif task.requirements["relevant"]:
            scenario = (
                "The reviewed candidate directly answers the current factual query."
            )
        else:
            scenario = (
                "The candidate is topically similar but does not answer the query."
            )
        return (
            scenario + " Return USE only for a relevant fact; otherwise return REJECT."
        )
    if task.subsystem == "agents":
        scenario = (
            "The subtasks edit overlapping files."
            if task.requirements["conflicting"]
            else "The subtasks are independent and have disjoint outputs."
        )
        return (
            scenario
            + " Return DELEGATE for independent work or SERIALIZE for conflicts."
        )
    return task.prompt + "\nIf the bounded workflow is valid, return CAPSLOCK_EVAL_OK."


def _simulate(
    task: EvaluationTask, candidate: PolicyCandidate
) -> tuple[bool, str | None, int, float, dict[str, float | int | bool | str]]:
    values, required = candidate.values, task.requirements
    metrics: dict[str, float | int | bool | str] = {
        "unauthorized_action": 0,
        "duplicate_destructive_side_effect": 0,
        "cross_workspace_leak": 0,
        "unresolved_agent_conflict": 0,
    }
    if task.subsystem == "runtime":
        rounds = int(required["rounds"])
        concurrency = int(values["tools.max_read_concurrency"])
        latency = (
            float(required["provider_latency"])
            + int(required["parallel_reads"]) / concurrency
        )
        checks = (
            rounds <= values["runtime.max_tool_rounds"],
            required["provider_latency"] <= values["providers.timeout_seconds"],
            required["repair_attempts"] <= values["tools.max_argument_repair_attempts"],
        )
        reason = (
            None
            if all(checks)
            else (
                "max_tool_rounds"
                if not checks[0]
                else "provider_timeout"
                if not checks[1]
                else "argument_repair_exhausted"
            )
        )
        return (
            all(checks),
            reason,
            min(rounds, int(values["runtime.max_tool_rounds"])),
            latency,
            metrics,
        )
    if task.subsystem == "context":
        compacted = required["pressure"] >= values["context.trigger_ratio"]
        retained = not compacted or (
            required["compaction_failures"] < values["context.max_compaction_failures"]
            and (
                required["required_turns"] <= values["context.preserve_recent_turns"]
                or required["required_tokens"]
                <= values["context.preserve_recent_tokens"]
            )
            and values["context.target_ratio"] < values["context.trigger_ratio"]
        )
        metrics["context_position"] = str(required["position"])
        metrics["context_recalled"] = retained
        return retained, None if retained else "context_not_recovered", 1, 0.01, metrics
    if task.subsystem == "loop":
        key = {
            "repeat": "loop_detection.consecutive_repeats",
            "failure": "loop_detection.failed_retries",
            "cycle": "loop_detection.cycle_repetitions",
        }[str(required["kind"])]
        detected = int(required["repetitions"]) >= int(values[key])
        if required["kind"] == "cycle":
            detected = detected and int(required["cycle_length"]) <= int(
                values["loop_detection.max_cycle_length"]
            )
        true_loop = bool(required["true_loop"])
        trials = int(required["labelled_sequences"])
        if true_loop:
            metrics["loop_true_cases"] = trials
            metrics["loop_true_detected"] = trials if detected else 0
        else:
            metrics["loop_legal_cases"] = trials
            metrics["loop_false_positives"] = trials if detected else 0
        return (
            detected if true_loop else not detected,
            "repeated_tool_call" if detected else None,
            int(required["repetitions"]),
            0.01 * int(required["repetitions"]),
            metrics,
        )
    if task.subsystem == "memory":
        relevant = bool(required["relevant"])
        final_score = sum(
            float(required[component]) * float(values[f"memory.{component}_weight"])
            for component in (
                "retrieval",
                "scope",
                "confidence",
                "freshness",
                "source_validity",
            )
        )
        candidate_selected = (
            required["rank"] <= values["memory.recall_limit"]
            and required["bytes"] <= values["memory.recall_bytes"]
            and required["semantic_score"] >= values["memory.semantic_threshold"]
            and final_score >= values["memory.recall_threshold"]
        )
        selected = candidate_selected and not bool(required["unsafe"])
        trials = int(required["labelled_memories"])
        metrics["memory_true_positive"] = trials if relevant and selected else 0
        metrics["memory_false_positive"] = trials if not relevant and selected else 0
        metrics["memory_eligible"] = trials if relevant else 0
        metrics["memory_calibration_trials"] = trials
        metrics["memory_calibration_error"] = (
            abs(float(required["calibrated_probability"]) - float(relevant)) * trials
        )
        return (
            (selected if relevant else not selected),
            None if selected or not relevant else "memory_not_recalled",
            1,
            0.005,
            metrics,
        )
    children = int(required["children"])
    concurrency = int(required["concurrency"])
    rounds = int(required["child_rounds"])
    success = (
        children <= values["agents.max_children"]
        and concurrency <= values["agents.max_concurrency"]
        and rounds <= values["agents.max_child_tool_rounds"]
    )
    latency = (
        rounds * max(1, children) / max(1, int(values["agents.max_concurrency"])) * 0.02
    )
    return (
        success,
        None if success else "child_budget_exhausted",
        rounds,
        latency,
        metrics,
    )


def write_evaluation(
    output: Path, report: dict[str, Any], samples: list[SampleResult]
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "samples.jsonl").write_text(
        "".join(
            json.dumps(item.as_dict(), ensure_ascii=False) + "\n" for item in samples
        ),
        encoding="utf-8",
    )
    recommendation = report["summary"].get("recommendation")
    if recommendation is not None:
        recommendation_manifest = {
            **recommendation,
            "report_hash": report["report_hash"],
        }
        (output / "recommendation.json").write_text(
            json.dumps(recommendation_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    _write_pareto_svg(
        output / "pareto.svg",
        report["summary"]["candidates"],
        set(report["summary"]["pareto_fingerprints"]),
    )
    command = " ".join(sys.argv)
    (output / "reproduce.txt").write_text(command + "\n", encoding="utf-8")


def _write_pareto_svg(
    path: Path, candidates: list[dict[str, Any]], pareto: set[str]
) -> None:
    width, height = 800, 480
    costs = [float(item["median_cost_usd"]) for item in candidates] or [0]
    latencies = [float(item["latency_seconds"]["p95"]) for item in candidates] or [0]
    max_cost, max_latency = max(costs) or 1, max(latencies) or 1
    circles = []
    for item in candidates:
        x = 50 + 700 * float(item["median_cost_usd"]) / max_cost
        y = 430 - 380 * float(item["latency_seconds"]["p95"]) / max_latency
        color = "#0a7" if item["fingerprint"] in pareto else "#999"
        circles.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="{color}"><title>{item["name"]}</title></circle>'
        )
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/><text x="20" y="25">Cost vs p95 latency (green = Pareto)</text>{"".join(circles)}</svg>\n'
    path.write_text(svg, encoding="utf-8")
