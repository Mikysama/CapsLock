"""Resumable, artifact-first external benchmark batch execution."""

from __future__ import annotations

import platform
import os
import shutil
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .adapters import AdapterContext, adapter_for
from .contracts import (
    ExternalTask,
    GraderStatus,
    ModelTrack,
    RunManifest,
    SuiteDefinition,
    TaskResult,
    canonical_hash,
    file_sha256,
)
from .io import read_json, write_json, write_jsonl
from .reporting import build_report
from .runtime import CapsLockRuntime, RuntimeLimits


class ExternalBatchRunner:
    def __init__(
        self,
        *,
        suite: SuiteDefinition,
        track: ModelTrack,
        tasks: list[ExternalTask],
        harness_root: Path,
        output_root: Path,
        executable: Path,
        wheel: Path,
        prompt_template: str,
        limits: RuntimeLimits,
        profile: str,
        repetitions: int,
        source_lock_hash: str,
        quarantine_sha256: str = "",
        quarantine_count: int = 0,
        run_id: str | None = None,
        defer_grade: bool = False,
    ) -> None:
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        if not tasks:
            raise ValueError("external batch must contain at least one task")
        if any(task.suite != suite.id for task in tasks):
            raise ValueError("all batch tasks must belong to the selected suite")
        if any(task.resource_class not in suite.resource_classes for task in tasks):
            raise ValueError(
                "batch task uses a resource class not declared by the suite"
            )
        if len({task.instance_id for task in tasks}) != len(tasks):
            raise ValueError("batch task instance IDs must be unique")
        if quarantine_count < 0:
            raise ValueError("quarantine_count cannot be negative")
        self.suite = suite
        self.track = track
        self.tasks = tasks
        self.harness_root = harness_root.resolve()
        self.output_root = output_root.resolve()
        self.executable = executable.resolve()
        self.wheel = wheel.resolve()
        self.prompt_template = prompt_template
        self.limits = limits
        self.profile = profile
        self.repetitions = repetitions
        self.source_lock_hash = source_lock_hash
        self.quarantine_sha256 = quarantine_sha256 or canonical_hash([])
        self.quarantine_count = quarantine_count
        self.run_id = run_id or _run_id(suite.id, track.name)
        self.defer_grade = defer_grade
        self.run_root = self.output_root / self.run_id
        self.adapter = adapter_for(suite)

    def run(self) -> dict[str, object]:
        _require_isolated_environment()
        self.run_root.mkdir(parents=True, exist_ok=True)
        manifest_path = self.run_root / "manifest.json"
        if manifest_path.exists():
            self._validate_resume_manifest(read_json(manifest_path))
        else:
            write_json(manifest_path, self._manifest().payload())
        self._write_state("running")
        runtime = CapsLockRuntime(
            executable=self.executable,
            track=self.track,
            limits=self.limits,
            prompt_template=self.prompt_template,
        )
        results = []
        try:
            for ordinal in range(1, self.repetitions + 1):
                for task in self.tasks:
                    results.append(self._run_task(runtime, task, ordinal))
        except BaseException:
            self._write_state("interrupted")
            raise
        write_jsonl(
            self.run_root / "results.jsonl", [item.payload() for item in results]
        )
        report = build_report(results, self.tasks)
        report.update(
            {
                "run_id": self.run_id,
                "profile": self.profile,
                "source_lock_hash": self.source_lock_hash,
                "official_task_count": self.suite.full_size,
                "executed_task_count": len(self.tasks),
                "quarantined_task_count": self.quarantine_count,
            }
        )
        report["report_hash"] = canonical_hash(
            {key: value for key, value in report.items() if key != "report_hash"}
        )
        write_json(self.run_root / "report.json", report)
        status = (
            "awaiting_grade"
            if any(item.failure_category == "awaiting_grader" for item in results)
            else "completed"
            if report["infrastructure_valid"]
            else "invalid"
        )
        self._write_state(status, completed=len(results))
        return report

    def _run_task(
        self, runtime: CapsLockRuntime, task: ExternalTask, ordinal: int
    ) -> TaskResult:
        task_root = self.run_root / "tasks" / task.instance_id / str(ordinal)
        result_path = task_root / "result.json"
        if result_path.exists():
            result = TaskResult.from_payload(read_json(result_path))
            if not result.infrastructure_error or (
                result.grader_status == GraderStatus.INFRASTRUCTURE_ERROR
            ):
                return result
        if task_root.exists():
            shutil.rmtree(task_root)
        task_root.mkdir(parents=True)
        workspace = task_root / "workspace"
        trajectory = task_root / "trajectory.jsonl"
        stderr = task_root / "stderr.log"
        public_task = task_root / "public-task.json"
        write_json(public_task, task.public_payload())
        context = AdapterContext(
            self.suite,
            self.harness_root,
            self.run_root,
            workspace,
            self.run_id,
            ordinal,
        )
        try:
            self.adapter.prepare(task, workspace)
            outcome = self.adapter.launch(
                task,
                runtime,
                workspace=workspace,
                state_home=task_root / "capslock-home",
                trajectory_path=trajectory,
                stderr_path=stderr,
            )
            artifact_name = (
                "solution.patch"
                if self.suite.artifact_kind.value == "git_patch"
                else "environment-state.json"
            )
            artifact = self.adapter.collect(task, workspace, task_root / artifact_name)
            if self.defer_grade:
                pending_grade = outcome.infrastructure_error is None
                result = TaskResult(
                    1,
                    self.suite.id,
                    self.source_lock_hash,
                    task.instance_id,
                    self.run_id,
                    ordinal,
                    self.track.name,
                    outcome.terminal_status,
                    GraderStatus.NOT_RUN,
                    False,
                    ("awaiting_grader" if pending_grade else "agent_infrastructure"),
                    outcome.stop_reason,
                    artifact.kind.value,
                    artifact.sha256,
                    outcome.input_tokens,
                    outcome.output_tokens,
                    _cost(self.track, outcome.input_tokens, outcome.output_tokens),
                    outcome.duration_seconds,
                    outcome.tool_rounds,
                    outcome.tool_calls,
                    artifact.changed_files,
                    artifact.changed_lines,
                    str(trajectory.relative_to(self.run_root)),
                    "",
                    outcome.infrastructure_error,
                    stderr_path=str(stderr.relative_to(self.run_root)),
                    peak_context_tokens=outcome.peak_context_tokens,
                    context_updates=outcome.context_updates,
                    context_compactions=outcome.context_compactions,
                    human_interventions=outcome.human_interventions,
                ).with_hash()
                write_json(result_path, result.payload())
                if not pending_grade:
                    self.adapter.cleanup(workspace)
                return result
            grade = self.adapter.grade(task, artifact, context)
            resolved = (
                grade.passed
                and grade.infrastructure_error is None
                and outcome.infrastructure_error is None
                and outcome.terminal_status == "completed"
                and outcome.stop_reason is None
            )
            failure = _failure_category(
                outcome.terminal_status,
                outcome.stop_reason,
                grade.passed,
                grade.infrastructure_error,
            )
            result = TaskResult(
                1,
                self.suite.id,
                self.source_lock_hash,
                task.instance_id,
                self.run_id,
                ordinal,
                self.track.name,
                outcome.terminal_status,
                (
                    GraderStatus.INFRASTRUCTURE_ERROR
                    if grade.infrastructure_error
                    else GraderStatus.PASSED
                    if grade.passed
                    else GraderStatus.FAILED
                ),
                resolved,
                failure,
                outcome.stop_reason,
                artifact.kind.value,
                artifact.sha256,
                outcome.input_tokens,
                outcome.output_tokens,
                _cost(self.track, outcome.input_tokens, outcome.output_tokens),
                outcome.duration_seconds,
                outcome.tool_rounds,
                outcome.tool_calls,
                artifact.changed_files,
                artifact.changed_lines,
                str(trajectory.relative_to(self.run_root)),
                str(grade.log_path.relative_to(self.run_root)),
                grade.infrastructure_error or outcome.infrastructure_error,
                stderr_path=str(stderr.relative_to(self.run_root)),
                peak_context_tokens=outcome.peak_context_tokens,
                context_updates=outcome.context_updates,
                context_compactions=outcome.context_compactions,
                human_interventions=outcome.human_interventions,
            ).with_hash()
        except Exception as exc:  # noqa: BLE001 - suite adapters are an error boundary
            stderr.parent.mkdir(parents=True, exist_ok=True)
            with stderr.open("a", encoding="utf-8") as handle:
                handle.write(f"{type(exc).__name__}: {exc}\n")
            result = TaskResult(
                1,
                self.suite.id,
                self.source_lock_hash,
                task.instance_id,
                self.run_id,
                ordinal,
                self.track.name,
                "infrastructure_error",
                GraderStatus.NOT_RUN,
                False,
                "infrastructure_error",
                None,
                self.suite.artifact_kind.value,
                None,
                0,
                0,
                0.0,
                0.0,
                0,
                0,
                0,
                0,
                str(trajectory.relative_to(self.run_root)),
                "",
                f"{type(exc).__name__}: {exc}",
                stderr_path=str(stderr.relative_to(self.run_root)),
            ).with_hash()
        write_json(result_path, result.payload())
        # A grader infrastructure failure keeps the workspace for `grade` retry.
        if result.grader_status != GraderStatus.INFRASTRUCTURE_ERROR:
            self.adapter.cleanup(workspace)
        return result

    def _manifest(self) -> RunManifest:
        task_ids = [task.instance_id for task in self.tasks]
        manifest = RunManifest(
            1,
            self.run_id,
            self.suite.id,
            self.source_lock_hash,
            self.profile,
            canonical_hash(task_ids),
            _git_commit(),
            file_sha256(self.wheel),
            self.track.name,
            self.track.provider,
            self.track.model,
            canonical_hash(self.prompt_template),
            "external-standard-v1",
            asdict(self.limits),
            self.repetitions,
            datetime.now(UTC).isoformat(),
            {
                "os": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "docker": _version(("docker", "--version")),
                "modal": _version(("modal", "--version")),
            },
            {
                "input_cost_per_million": self.track.input_cost_per_million,
                "output_cost_per_million": self.track.output_cost_per_million,
            },
            self.quarantine_sha256,
            self.quarantine_count,
        )
        return manifest.with_hash()

    def _validate_resume_manifest(self, existing: dict[str, object]) -> None:
        RunManifest(**existing).validate()
        current = self._manifest().payload()
        for key in (
            "suite",
            "suite_revision",
            "profile",
            "task_ids_sha256",
            "capslock_commit",
            "capslock_wheel_sha256",
            "model_track",
            "model",
            "prompt_sha256",
            "limits",
            "repetition_count",
            "quarantine_sha256",
            "quarantine_count",
        ):
            if existing.get(key) != current.get(key):
                raise RuntimeError(f"cannot resume run after {key} changed")

    def _write_state(self, status: str, *, completed: int | None = None) -> None:
        value: dict[str, object] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "status": status,
            "expected_results": len(self.tasks) * self.repetitions,
            "completed_results": completed if completed is not None else 0,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        value["state_hash"] = canonical_hash(value)
        write_json(self.run_root / "state.json", value)


def _run_id(suite: str, track: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"external-{suite}-{track}-{stamp}-{uuid4().hex[:8]}"


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _version(command: tuple[str, ...]) -> str:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def _cost(track: ModelTrack, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * track.input_cost_per_million
        + output_tokens * track.output_cost_per_million
    ) / 1_000_000


def _failure_category(
    terminal_status: str,
    stop_reason: str | None,
    grader_passed: bool,
    grader_error: str | None,
) -> str | None:
    if grader_error:
        return "grader_infrastructure"
    if terminal_status == "infrastructure_error":
        return "agent_infrastructure"
    if stop_reason:
        return stop_reason
    if terminal_status != "completed":
        return f"agent_{terminal_status}"
    if not grader_passed:
        return "official_tests_failed"
    return None


def _require_isolated_environment() -> None:
    if os.environ.get("CAPSLOCK_EVAL_ISOLATED") != "1":
        raise RuntimeError(
            "external rollouts require CAPSLOCK_EVAL_ISOLATED=1 inside a disposable task sandbox"
        )
    if os.environ.get("CAPSLOCK_EVAL_NETWORK_POLICY") not in {
        "provider-only",
        "task-allowlist",
    }:
        raise RuntimeError(
            "external rollouts require an enforced CAPSLOCK_EVAL_NETWORK_POLICY"
        )
