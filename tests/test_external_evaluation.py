from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from capslock.evaluation.external.adapters import adapter_for
from capslock.evaluation.external.catalog import load_catalog
from capslock.evaluation.external.cli import main as external_main
from capslock.evaluation.external.contracts import (
    ArtifactKind,
    ExternalTask,
    ModelTrack,
    SuiteDefinition,
    TaskResult,
    canonical_hash,
)
from capslock.evaluation.external.io import read_json, write_json, write_jsonl
from capslock.evaluation.external.registry import load_registry
from capslock.evaluation.external.quarantine import (
    QuarantineEntry,
    apply_quarantine,
    quarantine_hash,
)
from capslock.evaluation.external.reporting import build_report, compare_reports
from capslock.evaluation.external.runner import ExternalBatchRunner
from capslock.evaluation.external.runtime import RuntimeLimits, _parse_outcome
from capslock.evaluation.external.sampling import select_core_tasks

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = ROOT / "evaluations" / "external"


def test_external_registry_defines_pinned_suites_and_tracks() -> None:
    registry = load_registry(EXTERNAL / "suites.toml")

    assert set(registry.models) == {"flash", "pro"}
    assert len(registry.suites) == 6
    assert all(len(item.upstream_revision) == 40 for item in registry.suites.values())
    assert registry.suite("terminal_bench").full_split == "4.0.0"
    assert registry.suite("swebench_live").excluded_splits == ("windows",)


def test_catalog_rejects_hidden_fields_and_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "tasks.jsonl"
    row = task_row(tmp_path)
    row["hint"] = "secret"
    write_jsonl(path, [row])
    with pytest.raises(ValueError, match="unknown catalog fields"):
        load_catalog(path)

    row.pop("hint")
    row["problem_statement"] = {"text": "task", "test_patch": "secret"}
    write_jsonl(path, [row])
    with pytest.raises(ValueError, match="hidden evaluation field"):
        load_catalog(path)

    row["problem_statement"] = "task"
    write_jsonl(path, [row, row])
    with pytest.raises(ValueError, match="duplicate"):
        load_catalog(path)


def test_quarantine_is_hashed_restricted_and_task_scoped(tmp_path: Path) -> None:
    task = ExternalTask("setupbench", "one", "task", str(tmp_path))
    payload = {
        "schema_version": 1,
        "suite": "setupbench",
        "instance_id": "one",
        "upstream_revision": "a" * 40,
        "reason": "upstream_grader_defect",
        "observed_failure": "official harness crashes before grading",
        "reproduction_log_sha256": "b" * 64,
        "recorded_at": "2026-09-06T00:00:00+00:00",
        "reviewer": "evaluation-owner",
        "upstream_issue_url": "https://example.com/issues/1",
    }
    entry = QuarantineEntry(**payload, entry_hash=canonical_hash(payload))

    entry.validate(suite="setupbench", upstream_revision="a" * 40)
    assert apply_quarantine([task], [entry]) == []
    assert len(quarantine_hash([entry])) == 64

    invalid = QuarantineEntry(
        **{**payload, "reason": "agent_failed"},
        entry_hash=canonical_hash({**payload, "reason": "agent_failed"}),
    )
    with pytest.raises(ValueError, match="disallowed quarantine reason"):
        invalid.validate(suite="setupbench", upstream_revision="a" * 40)


def test_core_sampling_is_stable_unique_and_outcome_independent(tmp_path: Path) -> None:
    tasks = [
        ExternalTask(
            "setupbench",
            f"task-{index:03}",
            "configure it",
            str(tmp_path),
            repository=f"repo-{index % 4}",
            language=("python", "go", "rust")[index % 3],
            task_type=("repo", "database")[index % 2],
            gold_patch_size=index * 5,
        )
        for index in range(90)
    ]

    first, first_hash = select_core_tasks(tasks, size=60, seed=20260906)
    second, second_hash = select_core_tasks(
        list(reversed(tasks)), size=60, seed=20260906
    )

    assert [item.instance_id for item in first] == [item.instance_id for item in second]
    assert (
        first_hash
        == second_hash
        == canonical_hash([item.instance_id for item in first])
    )
    assert len({item.instance_id for item in first}) == 60
    assert {item.language for item in first} == {"python", "go", "rust"}


def test_core_sampling_balances_primary_strata_before_repositories(
    tmp_path: Path,
) -> None:
    tasks = []
    for index in range(80):
        tasks.append(
            ExternalTask(
                "setupbench",
                f"python-{index}",
                "configure it",
                str(tmp_path),
                repository=f"python-repo-{index % 10}",
                language="python",
            )
        )
    for index in range(20):
        tasks.append(
            ExternalTask(
                "setupbench",
                f"go-{index}",
                "configure it",
                str(tmp_path),
                repository="go-repo",
                language="go",
            )
        )

    selected, _ = select_core_tasks(tasks, size=40, seed=20260906)

    assert sum(task.language == "python" for task in selected) == 20
    assert sum(task.language == "go" for task in selected) == 20


@pytest.mark.parametrize(
    ("suite", "accepted", "rejected"),
    [
        ("swebench_verified", ["swebench", "eval", "verified"], ["pytest"]),
        ("swebench_live", ["python", "-m", "evaluation.evaluation"], ["pytest"]),
        (
            "multi_swe_bench",
            ["python", "-m", "multi_swe_bench.harness.run_evaluation"],
            ["pytest"],
        ),
        ("featurebench", ["fb", "eval"], ["pytest"]),
        ("terminal_bench", ["harbor", "run"], ["pytest"]),
        (
            "setupbench",
            ["python3", "setupbench/evaluation_harness.py"],
            ["pytest"],
        ),
    ],
)
def test_official_adapter_command_boundaries(
    suite: str, accepted: list[str], rejected: list[str]
) -> None:
    definition = load_registry(EXTERNAL / "suites.toml").suite(suite)
    adapter = adapter_for(definition)

    adapter._validate_grade_command(accepted)
    with pytest.raises(ValueError, match="official|accepted"):
        adapter._validate_grade_command(rejected)


def test_patch_collection_includes_staged_and_untracked_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "eval@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "External Eval"],
        check=True,
    )
    (source / "tracked.txt").write_text("before\n")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)
    workspace = tmp_path / "workspace"
    definition = load_registry(EXTERNAL / "suites.toml").suite("swebench_verified")
    adapter = adapter_for(definition)
    task = ExternalTask(
        definition.id,
        "patch-fixture",
        "change files",
        str(source),
    )
    adapter.prepare(task, workspace)
    (workspace / "tracked.txt").write_text("after\n")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    (workspace / "new.txt").write_text("new\n")
    (workspace / ".capslock").mkdir()
    (workspace / ".capslock" / "private.txt").write_text("private\n")

    artifact = adapter.collect(task, workspace, tmp_path / "solution.patch")
    patch = artifact.path.read_text()

    assert "tracked.txt" in patch
    assert "new.txt" in patch
    assert ".capslock" not in patch
    assert artifact.changed_files == 2


def test_runtime_outcome_requires_authoritative_terminal_jsonl() -> None:
    completed = json.dumps(
        {
            "event": "completed",
            "status": "completed",
            "terminal": True,
            "data": {
                "usage": {"input_tokens": 12, "output_tokens": 4},
                "governance": {"tool_rounds": 3, "tool_calls": 7},
            },
        }
    )

    outcome = _parse_outcome(f"noise\n{completed}\n", 0, 1.25)
    missing = _parse_outcome("not json\n", 0, 1.0)

    assert outcome.terminal_status == "completed"
    assert (outcome.input_tokens, outcome.output_tokens) == (12, 4)
    assert (outcome.tool_rounds, outcome.tool_calls) == (3, 7)
    assert (
        missing.infrastructure_error
        == "CapsLock JSONL did not contain a terminal event"
    )


def test_runtime_outcome_reads_tool_counts_from_budget_used() -> None:
    completed = json.dumps(
        {
            "event": "stopped",
            "status": "stopped",
            "terminal": True,
            "data": {
                "usage": {"input_tokens": 20, "output_tokens": 5},
                "budget": {"used": {"tool_rounds": 4, "tool_calls": 9}},
            },
        }
    )
    outcome = _parse_outcome(completed, 0, 1.0)
    assert (outcome.tool_rounds, outcome.tool_calls) == (4, 9)


def test_result_schema_hash_reporting_and_paired_comparison(tmp_path: Path) -> None:
    task = ExternalTask("setupbench", "one", "task", str(tmp_path), language="python")
    baseline = result("one", 1, False)
    candidate = result("one", 1, True)
    candidate_two = result("one", 2, True)
    candidate_three = result("one", 3, True)
    report = build_report([candidate, candidate_two, candidate_three], [task])
    comparison = compare_reports([candidate], [baseline], seed=1)

    assert report["resolve_rate"] == 1
    assert report["reliable_success_3"] == 1
    assert comparison["resolve_delta"] == 1
    assert candidate.result_hash == candidate.with_hash().result_hash

    for schema_name, payload in (("task-result-v1.json", candidate.payload()),):
        schema = json.loads((EXTERNAL / "schemas" / schema_name).read_text())
        jsonschema.validate(payload, schema)


def test_runner_is_resumable_and_keeps_hidden_grader_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_source = tmp_path / "source"
    workspace_source.mkdir()
    (workspace_source / "README.md").write_text("fixture\n")
    harness = tmp_path / "harness"
    grader = harness / "setupbench" / "evaluation_harness.py"
    grader.parent.mkdir(parents=True)
    grader.write_text(
        "import os\nprint(os.environ['DEEPSEEK_API_KEY'])\nprint('Setup successful')\n"
    )
    executable = fake_capslock(tmp_path)
    wheel = tmp_path / "capslock.whl"
    wheel.write_bytes(b"wheel")
    counter = tmp_path / "calls.txt"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "credential-that-must-not-be-logged")
    monkeypatch.setenv("FAKE_CAPSLOCK_COUNTER", str(counter))
    monkeypatch.setenv("CAPSLOCK_EVAL_ISOLATED", "1")
    monkeypatch.setenv("CAPSLOCK_EVAL_NETWORK_POLICY", "provider-only")
    task = ExternalTask(
        "setupbench",
        "fixture",
        "configure fixture",
        str(workspace_source),
        grader={
            "command": ["python3", "setupbench/evaluation_harness.py"],
            "success_substring": "Setup successful",
        },
    )
    definition = SuiteDefinition(
        "setupbench",
        "setupbench",
        ArtifactKind.ENVIRONMENT_STATE,
        "https://github.com/microsoft/SetupBench.git",
        "a" * 40,
        "MIT",
        1,
        1,
        ("python",),
    )
    track = ModelTrack(
        "flash",
        "deepseek",
        "deepseek-v4-flash",
        "https://api.deepseek.com",
        "DEEPSEEK_API_KEY",
    )

    runner = ExternalBatchRunner(
        suite=definition,
        track=track,
        tasks=[task],
        harness_root=harness,
        output_root=tmp_path / "results",
        executable=executable,
        wheel=wheel,
        prompt_template="Do this: {problem_statement}",
        limits=RuntimeLimits(32, 256, 100000, 60),
        profile="smoke",
        repetitions=1,
        source_lock_hash="b" * 64,
        run_id="fixed-run",
    )
    first = runner.run()
    second = runner.run()

    assert first["resolve_rate"] == second["resolve_rate"] == 1
    assert counter.read_text().splitlines() == ["init", "exec"]
    run_root = tmp_path / "results" / "fixed-run"
    stored = read_json(run_root / "tasks" / "fixture" / "1" / "public-task.json")
    assert "grader" not in stored
    all_logs = "".join(
        path.read_text(errors="replace")
        for path in run_root.rglob("*")
        if path.is_file()
    )
    assert "credential-that-must-not-be-logged" not in all_logs


def test_deferred_grade_invokes_official_harness_and_cleans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = load_registry(EXTERNAL / "suites.toml")
    definition = registry.suite("setupbench")
    workspace_source = tmp_path / "source"
    workspace_source.mkdir()
    (workspace_source / "README.md").write_text("fixture\n")
    cache = tmp_path / "cache"
    harness = cache / "harnesses" / definition.id
    grader = harness / "setupbench" / "evaluation_harness.py"
    grader.parent.mkdir(parents=True)
    grader.write_text(
        "import os\nprint(os.environ['DEEPSEEK_API_KEY'])\nprint('Setup successful')\n"
    )
    lock = {
        "schema_version": 1,
        "suite": definition.id,
        "upstream_repo": definition.upstream_repo,
        "upstream_revision": definition.upstream_revision,
        "dataset_revisions": {},
        "adapter": definition.adapter,
        "artifact_kind": definition.artifact_kind.value,
        "license": definition.license,
        "core_split": definition.core_split,
        "full_split": definition.full_split,
        "excluded_splits": list(definition.excluded_splits),
        "resource_classes": list(definition.resource_classes),
        "created_at": "2026-09-06T00:00:00+00:00",
    }
    task = ExternalTask(
        definition.id,
        "deferred-fixture",
        "configure fixture",
        str(workspace_source),
        grader={
            "command": ["python3", "setupbench/evaluation_harness.py"],
            "success_substring": "Setup successful",
        },
    )
    catalog = tmp_path / "catalog.jsonl"
    write_jsonl(catalog, [task.manifest_payload()])
    lock.update(
        {
            "task_count": 1,
            "task_ids_sha256": canonical_hash([task.instance_id]),
            "task_manifest_sha256": canonical_hash([task.manifest_payload()]),
            "grader_config_sha256": canonical_hash(
                {task.instance_id: task.manifest_payload()["grader"]}
            ),
        }
    )
    lock["lock_hash"] = canonical_hash(lock)
    write_json(cache / "locks" / f"{definition.id}.json", lock)
    executable = fake_capslock(tmp_path)
    wheel = tmp_path / "capslock.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-credential")
    monkeypatch.setenv("FAKE_CAPSLOCK_COUNTER", str(tmp_path / "calls.txt"))
    monkeypatch.setenv("CAPSLOCK_EVAL_ISOLATED", "1")
    monkeypatch.setenv("CAPSLOCK_EVAL_NETWORK_POLICY", "provider-only")
    runner = ExternalBatchRunner(
        suite=definition,
        track=registry.model("flash"),
        tasks=[task],
        harness_root=harness,
        output_root=tmp_path / "results",
        executable=executable,
        wheel=wheel,
        prompt_template="Do this: {problem_statement}",
        limits=RuntimeLimits(32, 256, 100000, 60),
        profile="smoke",
        repetitions=1,
        source_lock_hash=str(lock["lock_hash"]),
        run_id="deferred-run",
        defer_grade=True,
    )

    report = runner.run()
    run_root = tmp_path / "results" / "deferred-run"
    task_root = run_root / "tasks" / task.instance_id / "1"
    pending = read_json(task_root / "result.json")

    assert report["infrastructure_valid"] is False
    assert pending["grader_status"] == "not_run"
    assert (task_root / "workspace").is_dir()
    assert (
        external_main(
            [
                "--registry",
                str(EXTERNAL / "suites.toml"),
                "grade",
                "--cache",
                str(cache),
                str(run_root),
                "--catalog",
                str(catalog),
            ]
        )
        == 0
    )
    graded = read_json(task_root / "result.json")
    assert graded["grader_status"] == "passed"
    assert graded["resolved"] is True
    assert not (task_root / "workspace").exists()
    assert "test-credential" not in (task_root / "grader.log").read_text()
    for schema_name, payload in (
        ("run-manifest-v1.json", read_json(run_root / "manifest.json")),
        ("suite-manifest-v1.json", read_json(cache / "locks" / "setupbench.json")),
        ("task-result-v1.json", graded),
    ):
        schema = json.loads((EXTERNAL / "schemas" / schema_name).read_text())
        jsonschema.validate(payload, schema)


def task_row(tmp_path: Path) -> dict[str, object]:
    return {
        "suite": "setupbench",
        "instance_id": "one",
        "problem_statement": "task",
        "workspace_source": str(tmp_path),
        "grader": {"command": ["python", "setupbench/evaluation_harness.py"]},
    }


def result(instance_id: str, ordinal: int, success: bool) -> TaskResult:
    return TaskResult(
        1,
        "setupbench",
        "b" * 64,
        instance_id,
        "run",
        ordinal,
        "flash",
        "completed",
        "passed" if success else "failed",
        success,
        None if success else "official_tests_failed",
        None,
        "environment_state",
        "c" * 64,
        10,
        5,
        0.1,
        1.0,
        1,
        1,
        0,
        0,
        "trajectory.jsonl",
        "grader.log",
    ).with_hash()


def fake_capslock(tmp_path: Path) -> Path:
    path = tmp_path / "fake-capslock"
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
workspace = pathlib.Path(args[args.index('--workspace') + 1])
command = 'init' if 'init' in args else 'exec'
with open(os.environ['FAKE_CAPSLOCK_COUNTER'], 'a', encoding='utf-8') as handle:
    handle.write(command + '\\n')
if command == 'init':
    root = workspace / '.capslock'
    root.mkdir(parents=True, exist_ok=True)
    (root / 'config.toml').write_text('config_version = 13\\n')
else:
    print(os.environ['CAPSLOCK_EVAL_API_KEY'], file=sys.stderr)
    print(json.dumps({
        'event': 'completed',
        'status': 'completed',
        'terminal': True,
        'data': {
            'usage': {'input_tokens': 10, 'output_tokens': 5},
            'governance': {'tool_rounds': 1, 'tool_calls': 1},
        },
    }))
"""
    )
    path.chmod(0o755)
    return path
