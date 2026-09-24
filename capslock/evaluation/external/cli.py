"""Command-line controller for External Eval v1."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .adapters import AdapterContext, Artifact, adapter_for
from .catalog import load_catalog
from .contracts import (
    ArtifactKind,
    GraderStatus,
    RunManifest,
    TaskResult,
    canonical_hash,
    file_sha256,
)
from .io import read_json, read_jsonl, write_json, write_jsonl
from .quarantine import apply_quarantine, load_quarantine, quarantine_hash
from .registry import ExternalRegistry, load_registry
from .reporting import build_report, compare_reports
from .runner import ExternalBatchRunner
from .runtime import RuntimeLimits, create_runtime_environment
from .sampling import select_core_tasks
from .sources import (
    sync_harness,
    validate_catalog_lock,
    validate_public_repository_url,
    validate_source_lock,
    write_source_lock,
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY = ROOT / "evaluations" / "external" / "suites.toml"
DEFAULT_PROMPT = (
    ROOT / "evaluations" / "external" / "prompts" / "autonomous-coding-v1.md"
)
DEFAULT_CACHE = Path(
    os.environ.get("CAPSLOCK_EVAL_CACHE", "/tmp/capslock-external-eval")
)
DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get("CAPSLOCK_EVAL_RUNTIME_ROOT", "/opt/capslock")
)
DEFAULT_QUARANTINE = ROOT / "evaluations" / "external" / "quarantine"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evaluate_external.py")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="Pin upstream harnesses and datasets")
    _common_cache(sync)
    sync.add_argument("--suite", action="append", choices=_suite_choices())
    sync.add_argument(
        "--catalog",
        action="append",
        default=[],
        metavar="SUITE=JSONL",
        help="Import a normalized task catalog into the external cache",
    )

    doctor = subparsers.add_parser("doctor", help="Check external eval prerequisites")
    _common_cache(doctor)
    doctor.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    doctor.add_argument("--suite", action="append", choices=_suite_choices())
    doctor.add_argument("--quarantine-root", type=Path, default=DEFAULT_QUARANTINE)
    doctor.add_argument("--json", action="store_true")

    run = subparsers.add_parser("run", help="Run and grade an external batch")
    _run_arguments(run)
    resume = subparsers.add_parser("resume", help="Resume an interrupted batch")
    _run_arguments(resume)
    resume.add_argument("--run-id", required=True)

    grade = subparsers.add_parser("grade", help="Grade deferred artifacts")
    _common_cache(grade)
    grade.add_argument("run_directory", type=Path)
    grade.add_argument("--catalog", type=Path, required=True)

    report = subparsers.add_parser("report", help="Compare a run with a baseline")
    report.add_argument("run_directory", type=Path)
    report.add_argument("--catalog", type=Path, required=True)
    report.add_argument("--baseline", type=Path)
    report.add_argument("--seed", type=int, default=20260906)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = load_registry(args.registry.resolve())
        if args.command == "sync":
            return _sync(args, registry)
        if args.command == "doctor":
            return _doctor(args, registry)
        if args.command in {"run", "resume"}:
            return _run(args, registry)
        if args.command == "grade":
            return _rebuild_report(args)
        if args.command == "report":
            return _report(args)
        raise ValueError(f"unknown command: {args.command}")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"external evaluation error: {exc}", file=sys.stderr)
        return 2


def _common_cache(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)


def _run_arguments(parser: argparse.ArgumentParser) -> None:
    _common_cache(parser)
    parser.add_argument("--suite", required=True, choices=_suite_choices())
    parser.add_argument("--model-track", required=True, choices=("flash", "pro"))
    parser.add_argument("--profile", choices=("smoke", "core", "full"), required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--quarantine-root", type=Path, default=DEFAULT_QUARANTINE)
    parser.add_argument(
        "--defer-grade",
        action="store_true",
        help="Collect artifacts now and invoke the official grader separately",
    )


def _suite_choices() -> tuple[str, ...]:
    return (
        "swebench_verified",
        "swebench_live",
        "multi_swe_bench",
        "featurebench",
        "terminal_bench",
        "setupbench",
    )


def _selected(args: argparse.Namespace, registry: ExternalRegistry) -> list[str]:
    return args.suite or list(registry.suites)


def _sync(args: argparse.Namespace, registry: ExternalRegistry) -> int:
    cache = args.cache.expanduser().resolve()
    imported = _catalog_arguments(args.catalog)
    report = {"schema_version": 1, "suites": {}}
    failed = False
    for suite_id in _selected(args, registry):
        definition = registry.suite(suite_id)
        validate_public_repository_url(definition.upstream_repo)
        try:
            harness = sync_harness(definition, cache)
            catalog_path = None
            if suite_id in imported:
                tasks = load_catalog(imported[suite_id], suite=suite_id)
                catalog_path = cache / "catalogs" / f"{suite_id}.jsonl"
                write_jsonl(catalog_path, [task.manifest_payload() for task in tasks])
            else:
                cached_catalog = cache / "catalogs" / f"{suite_id}.jsonl"
                tasks = (
                    load_catalog(cached_catalog, suite=suite_id)
                    if cached_catalog.is_file()
                    else None
                )
                catalog_path = cached_catalog if tasks is not None else None
            lock = write_source_lock(definition, cache, tasks=tasks)
            report["suites"][suite_id] = {
                "status": "ready",
                "harness": str(harness),
                "lock": str(lock),
                "catalog": str(catalog_path) if catalog_path else None,
            }
        except Exception as exc:  # noqa: BLE001 - report all suite sync failures
            failed = True
            report["suites"][suite_id] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
    report["report_hash"] = canonical_hash(report)
    write_json(cache / "sync-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def _doctor(args: argparse.Namespace, registry: ExternalRegistry) -> int:
    cache = args.cache.expanduser().resolve()
    issues = []
    for suite_id in _selected(args, registry):
        definition = registry.suite(suite_id)
        harness = cache / "harnesses" / suite_id
        lock = cache / "locks" / f"{suite_id}.json"
        catalog = cache / "catalogs" / f"{suite_id}.jsonl"
        if not harness.is_dir():
            issues.append({"suite": suite_id, "code": "harness_missing"})
        else:
            head = _git_head(harness)
            if head != definition.upstream_revision:
                issues.append(
                    {
                        "suite": suite_id,
                        "code": "harness_revision",
                        "expected": definition.upstream_revision,
                        "actual": head,
                    }
                )
        if not lock.is_file():
            issues.append({"suite": suite_id, "code": "source_lock_missing"})
        else:
            try:
                validate_source_lock(read_json(lock), suite=suite_id)
            except ValueError as exc:
                issues.append(
                    {
                        "suite": suite_id,
                        "code": "source_lock_invalid",
                        "error": str(exc),
                    }
                )
        if not catalog.is_file():
            issues.append({"suite": suite_id, "code": "catalog_missing"})
        else:
            try:
                catalog_tasks = load_catalog(catalog, suite=suite_id)
                count = len(catalog_tasks)
                if count < definition.core_size:
                    issues.append(
                        {
                            "suite": suite_id,
                            "code": "catalog_too_small",
                            "count": count,
                            "required": definition.core_size,
                        }
                    )
                if lock.is_file():
                    validate_catalog_lock(
                        read_json(lock), catalog_tasks, suite=suite_id
                    )
                entries = load_quarantine(
                    args.quarantine_root.resolve(),
                    suite=suite_id,
                    upstream_revision=definition.upstream_revision,
                )
                apply_quarantine(catalog_tasks, entries)
            except ValueError as exc:
                issues.append(
                    {"suite": suite_id, "code": "catalog_invalid", "error": str(exc)}
                )
        issues.extend(
            {"suite": suite_id, "code": "prerequisite", "error": value}
            for value in adapter_for(definition).doctor(harness)
        )
    for name, track in registry.models.items():
        if not os.environ.get(track.credential_env):
            issues.append(
                {
                    "model_track": name,
                    "code": "credential_missing",
                    "name": track.credential_env,
                }
            )
        if track.input_cost_per_million == track.output_cost_per_million == 0:
            issues.append({"model_track": name, "code": "pricing_unconfigured"})
    if os.environ.get("CAPSLOCK_EVAL_ISOLATED") != "1":
        issues.append(
            {"code": "isolation_unasserted", "name": "CAPSLOCK_EVAL_ISOLATED"}
        )
    if os.environ.get("CAPSLOCK_EVAL_NETWORK_POLICY") not in {
        "provider-only",
        "task-allowlist",
    }:
        issues.append(
            {
                "code": "network_policy_unasserted",
                "name": "CAPSLOCK_EVAL_NETWORK_POLICY",
            }
        )
    runtime_parent = _existing_parent(args.runtime_root.expanduser().resolve())
    if not os.access(runtime_parent, os.W_OK):
        issues.append(
            {
                "code": "runtime_root_unwritable",
                "path": str(args.runtime_root),
            }
        )
    result = {
        "schema_version": 1,
        "status": "ready" if not issues else "failed",
        "issues": issues,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif issues:
        for issue in issues:
            print(json.dumps(issue, ensure_ascii=False, sort_keys=True))
    else:
        print("External evaluation prerequisites are ready.")
    return 1 if issues else 0


def _run(args: argparse.Namespace, registry: ExternalRegistry) -> int:
    cache = args.cache.expanduser().resolve()
    suite = registry.suite(args.suite)
    track = registry.model(args.model_track)
    catalog_path = args.catalog or cache / "catalogs" / f"{suite.id}.jsonl"
    catalog_tasks = load_catalog(catalog_path.resolve(), suite=suite.id)
    lock = read_json(cache / "locks" / f"{suite.id}.json")
    lock_hash = validate_source_lock(lock, suite=suite.id)
    validate_catalog_lock(lock, catalog_tasks, suite=suite.id)
    quarantine = load_quarantine(
        args.quarantine_root.resolve(),
        suite=suite.id,
        upstream_revision=suite.upstream_revision,
    )
    tasks = apply_quarantine(catalog_tasks, quarantine)
    if args.profile == "smoke":
        tasks, _ = select_core_tasks(tasks, size=5, seed=registry.defaults.core_seed)
        repetitions = 1
    elif args.profile == "core":
        tasks, _ = select_core_tasks(
            tasks, size=suite.core_size, seed=registry.defaults.core_seed
        )
        repetitions = registry.defaults.core_repetitions
    else:
        if suite.full_size is not None and len(catalog_tasks) != suite.full_size:
            raise ValueError(
                f"full profile for {suite.id} requires exactly {suite.full_size} tasks"
            )
        repetitions = registry.defaults.full_repetitions
    wheel = args.wheel.expanduser().resolve()
    runtime_root = args.runtime_root.expanduser().resolve() / file_sha256(wheel)
    executable = runtime_root / (
        "Scripts/capslock.exe" if os.name == "nt" else "bin/capslock"
    )
    if not executable.is_file():
        executable = create_runtime_environment(wheel, runtime_root)
    runner = ExternalBatchRunner(
        suite=suite,
        track=track,
        tasks=tasks,
        harness_root=cache / "harnesses" / suite.id,
        output_root=args.output.expanduser(),
        executable=executable,
        wheel=wheel,
        prompt_template=args.prompt.read_text(encoding="utf-8"),
        limits=RuntimeLimits(
            registry.defaults.max_tool_rounds,
            registry.defaults.max_tool_calls,
            registry.defaults.max_tokens,
            registry.defaults.max_duration_seconds,
        ),
        profile=args.profile,
        repetitions=repetitions,
        source_lock_hash=lock_hash,
        quarantine_sha256=quarantine_hash(quarantine),
        quarantine_count=len(quarantine),
        run_id=getattr(args, "run_id", None),
        defer_grade=args.defer_grade,
    )
    report = runner.run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if args.defer_grade or report["infrastructure_valid"] else 1


def _rebuild_report(args: argparse.Namespace) -> int:
    _require_grading_environment()
    run = args.run_directory.resolve()
    manifest = RunManifest(**read_json(run / "manifest.json"))
    manifest.validate()
    suite = load_registry(args.registry.resolve()).suite(manifest.suite)
    lock = read_json(args.cache.expanduser().resolve() / "locks" / f"{suite.id}.json")
    lock_hash = validate_source_lock(lock, suite=suite.id)
    if lock_hash != manifest.suite_revision:
        raise ValueError("run manifest and source lock revisions differ")
    tasks = load_catalog(args.catalog.resolve(), suite=suite.id)
    validate_catalog_lock(lock, tasks, suite=suite.id)
    task_index = {task.instance_id: task for task in tasks}
    results = _results(run / "results.jsonl")
    missing = sorted({item.instance_id for item in results} - set(task_index))
    if missing:
        raise ValueError(f"catalog is missing run tasks: {', '.join(missing)}")
    if any(
        item.suite != manifest.suite
        or item.suite_revision != manifest.suite_revision
        or item.run_id != manifest.run_id
        or item.model_track != manifest.model_track
        for item in results
    ):
        raise ValueError("task results do not match the run manifest")
    adapter = adapter_for(suite)
    harness = args.cache.expanduser().resolve() / "harnesses" / suite.id
    updated = list(results)
    for index, result in enumerate(results):
        if result.failure_category != "awaiting_grader" and not (
            result.grader_status == GraderStatus.INFRASTRUCTURE_ERROR
            and result.artifact_sha256
        ):
            continue
        task = task_index.get(result.instance_id)
        if task is None:
            raise ValueError(f"catalog is missing deferred task {result.instance_id}")
        task_root = run / "tasks" / result.instance_id / str(result.run_ordinal)
        workspace = task_root / "workspace"
        if not workspace.is_dir():
            raise ValueError(f"deferred workspace is missing: {workspace}")
        artifact_path = task_root / (
            "solution.patch"
            if result.artifact_kind == ArtifactKind.GIT_PATCH
            else "environment-state.json"
        )
        if (
            not artifact_path.is_file()
            or file_sha256(artifact_path) != result.artifact_sha256
        ):
            raise ValueError(
                f"deferred artifact is missing or changed: {artifact_path}"
            )
        artifact = Artifact(
            ArtifactKind(result.artifact_kind),
            artifact_path,
            result.artifact_sha256,
            result.changed_files,
            result.changed_lines,
        )
        context = AdapterContext(
            suite,
            harness,
            run,
            workspace,
            manifest.run_id,
            result.run_ordinal,
        )
        grade = adapter.grade(task, artifact, context)
        agent_infrastructure_error = (
            None
            if result.grader_status == GraderStatus.INFRASTRUCTURE_ERROR
            else result.infrastructure_error
        )
        grader_status = (
            GraderStatus.INFRASTRUCTURE_ERROR
            if grade.infrastructure_error
            else GraderStatus.PASSED
            if grade.passed
            else GraderStatus.FAILED
        )
        updated_result = replace(
            result,
            grader_status=grader_status,
            resolved=(
                grade.passed
                and grade.infrastructure_error is None
                and result.agent_terminal_status == "completed"
                and result.stop_reason is None
                and agent_infrastructure_error is None
            ),
            failure_category=(
                "grader_infrastructure"
                if grade.infrastructure_error
                else result.stop_reason
                if result.stop_reason
                else f"agent_{result.agent_terminal_status}"
                if result.agent_terminal_status != "completed"
                else None
                if grade.passed and agent_infrastructure_error is None
                else "official_tests_failed"
            ),
            grader_log_path=str(grade.log_path.relative_to(run)),
            infrastructure_error=grade.infrastructure_error
            or agent_infrastructure_error,
            result_hash="",
        ).with_hash()
        updated[index] = updated_result
        write_json(task_root / "result.json", updated_result.payload())
        write_jsonl(run / "results.jsonl", [item.payload() for item in updated])
        if not grade.infrastructure_error:
            adapter.cleanup(workspace)
    report = build_report(updated, [task_index[item.instance_id] for item in updated])
    report.update(
        {
            "run_id": manifest.run_id,
            "profile": manifest.profile,
            "source_lock_hash": manifest.suite_revision,
            "official_task_count": suite.full_size,
            "executed_task_count": len({item.instance_id for item in updated}),
            "quarantined_task_count": manifest.quarantine_count,
        }
    )
    report["report_hash"] = canonical_hash(
        {key: value for key, value in report.items() if key != "report_hash"}
    )
    write_json(run / "report.json", report)
    state = {
        "schema_version": 1,
        "run_id": manifest.run_id,
        "status": "completed" if report["infrastructure_valid"] else "invalid",
        "expected_results": len(updated),
        "completed_results": len(updated),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    state["state_hash"] = canonical_hash(state)
    write_json(run / "state.json", state)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["infrastructure_valid"] else 1


def _report(args: argparse.Namespace) -> int:
    run = args.run_directory.resolve()
    tasks = load_catalog(args.catalog.resolve())
    results = _results(run / "results.jsonl")
    manifest = RunManifest(**read_json(run / "manifest.json"))
    manifest.validate()
    report = build_report(results, tasks)
    if args.baseline:
        report["comparison"] = compare_reports(
            results,
            _results(args.baseline.resolve() / "results.jsonl"),
            seed=args.seed,
        )
    report.update(
        {
            "run_id": manifest.run_id,
            "profile": manifest.profile,
            "source_lock_hash": manifest.suite_revision,
            "executed_task_count": len({item.instance_id for item in results}),
            "quarantined_task_count": manifest.quarantine_count,
            "official_task_count": load_registry(args.registry.resolve())
            .suite(manifest.suite)
            .full_size,
        }
    )
    report["report_hash"] = canonical_hash(
        {key: value for key, value in report.items() if key != "report_hash"}
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _results(path: Path) -> list[TaskResult]:
    return [TaskResult.from_payload(row) for row in read_jsonl(path)]


def _catalog_arguments(values: list[str]) -> dict[str, Path]:
    parsed = {}
    for value in values:
        suite, separator, raw_path = value.partition("=")
        if not separator or suite not in _suite_choices() or not raw_path:
            raise ValueError("--catalog must use SUITE=JSONL")
        if suite in parsed:
            raise ValueError(f"duplicate catalog argument for {suite}")
        parsed[suite] = Path(raw_path).expanduser().resolve()
    return parsed


def _git_head(path: Path) -> str:
    completed = subprocess_run(("git", "-C", str(path), "rev-parse", "HEAD"))
    return completed.strip()


def subprocess_run(command: tuple[str, ...]) -> str:
    import subprocess

    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=30
    )
    return completed.stdout if completed.returncode == 0 else ""


def _require_grading_environment() -> None:
    if os.environ.get("CAPSLOCK_EVAL_ISOLATED") != "1":
        raise RuntimeError(
            "official grading requires CAPSLOCK_EVAL_ISOLATED=1 inside the task sandbox"
        )


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


if __name__ == "__main__":
    raise SystemExit(main())
