"""Offline gates and selection contracts; never invoke a paid provider."""

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

from capslock.evaluation.external.contracts import ExternalTask
from capslock.evaluation.external.reporting import build_report
from capslock.tooling.contracts import ToolOutcome, define_tool
from capslock.tooling.executor import ToolRuntime
from tests.test_external_evaluation import result


def test_filtered_selection_pins_discovered_tool_over_soft_budget():
    async def execute(context, arguments):
        return ToolOutcome.success(arguments)

    tools = [
        define_tool(name, "Read files", {"type": "object"}, execute)
        for name in ("ask_user", "search_tools", "read_file")
    ]
    tools.append(
        define_tool(
            "plugin__lookup",
            "Lookup records",
            {"type": "object"},
            execute,
            deferred=True,
        )
    )
    runtime = ToolRuntime(tools, selection_mode="filtered", schema_budget_tokens=1)
    runtime.discover(["plugin__lookup"])
    schemas, names = runtime.model_schemas("read files")
    assert {"ask_user", "search_tools", "plugin__lookup"} == set(names)
    assert set(names) == {item["function"]["name"] for item in schemas}


def test_filtered_selection_budgets_optional_schemas():
    async def execute(context, arguments):
        return ToolOutcome.success(arguments)

    runtime = ToolRuntime(
        [
            define_tool(name, "read " * 1000, {"type": "object"}, execute)
            for name in ("read_one", "read_two")
        ],
        selection_mode="filtered",
        schema_budget_tokens=1,
    )
    schemas, names = runtime.model_schemas("read")
    assert schemas == [] and names == ()


def test_external_report_cost_per_success_excludes_infrastructure():
    task = ExternalTask("setupbench", "one", "task", "/tmp/task")
    values = [
        replace(result("one", 1, True), cost_usd=2),
        replace(result("one", 2, False), cost_usd=3),
        replace(
            result("one", 3, False),
            cost_usd=99,
            infrastructure_error="offline",
            grader_status="infrastructure_error",
        ),
    ]
    report = build_report(values, [task])
    assert report["cost_per_successful_task_usd"] == 5
    assert report["valid_attempt_count"] == 2
    assert report["infrastructure_failure_count"] == 1
    assert report["valid_resolve_rate"] == 0.5
    assert build_report(values[1:], [task])["cost_per_successful_task_usd"] is None


def test_offline_manifest_has_sixty_distinct_executable_scenarios():
    from capslock.evaluation.offline import load_manifest

    root = Path(__file__).resolve().parents[1]
    manifest = load_manifest(root / "evaluations/offline-regression-v1.json", root=root)
    scenarios = manifest["scenarios"]
    assert len(scenarios) == len({item["nodeid"] for item in scenarios}) == 60
    assert sorted(Counter(item["group"] for item in scenarios).values()) == [12] * 5
    assert all(
        item["fixtures"] and item["input"] and item["expected"] for item in scenarios
    )


def test_offline_manifest_rejects_non_test_path(tmp_path):
    from capslock.evaluation.offline import load_manifest
    import pytest

    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "offline_kernel_regression",
                "scenarios": [
                    {
                        "id": "bad",
                        "group": "a",
                        "nodeid": "../outside.py::test_bad",
                        "fixtures": ["local"],
                        "input": "x",
                        "expected": "y",
                        "grader": "pytest_assertions",
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="test path"):
        load_manifest(path, root=tmp_path)


def test_external_dry_run_is_exposed_without_required_wheel_output():
    from capslock.evaluation.external.cli import build_parser

    args = build_parser().parse_args(
        [
            "run",
            "--suite",
            "setupbench",
            "--model-track",
            "flash",
            "--profile",
            "smoke",
            "--dry-run",
        ]
    )
    assert args.dry_run and args.wheel is None and args.output is None


def test_external_dry_run_validates_locked_catalog_and_cost_without_launch(
    tmp_path, capsys
):
    from capslock.evaluation.external.cli import _run, build_parser
    from capslock.evaluation.external.registry import load_registry
    from capslock.evaluation.external.sources import write_source_lock
    from capslock.evaluation.external.io import write_jsonl
    import pytest

    root = Path(__file__).resolve().parents[1]
    registry = load_registry(root / "evaluations/external/suites.toml")
    definition = replace(registry.suite("setupbench"), dataset_ids=())
    registry = replace(registry, suites={**registry.suites, "setupbench": definition})
    tasks = [
        ExternalTask("setupbench", f"task-{index}", "local task", str(tmp_path))
        for index in range(5)
    ]
    catalog = tmp_path / "catalogs/setupbench.jsonl"
    write_jsonl(catalog, [task.manifest_payload() for task in tasks])
    write_source_lock(definition, tmp_path, tasks=tasks)
    args = build_parser().parse_args(
        [
            "run",
            "--suite",
            "setupbench",
            "--model-track",
            "flash",
            "--profile",
            "smoke",
            "--dry-run",
            "--cache",
            str(tmp_path),
            "--quarantine-root",
            str(tmp_path / "quarantine"),
        ]
    )
    assert _run(args, registry) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider_calls"] == 0
    assert len(report["task_ids"]) == 5
    assert report["token_cost_upper_bound_usd"] > 0
    assert not (tmp_path / "harnesses").exists()
    args.max_cost_usd = 0
    with pytest.raises(ValueError, match="cost upper bound"):
        _run(args, registry)


def test_offline_runner_distinguishes_skip_failure_and_pass(tmp_path):
    from capslock.evaluation.offline import run_scenario

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_fixture.py").write_text(
        "import pytest\ndef test_pass(): assert 1 == 1\ndef test_fail(): assert 1 == 2\ndef test_skip(): pytest.skip('missing dependency')\n"
    )
    for name, status in (
        ("pass", "passed"),
        ("fail", "failed"),
        ("skip", "infrastructure_error"),
    ):
        report = run_scenario(
            {
                "id": name,
                "group": "fixture",
                "nodeid": f"tests/test_fixture.py::test_{name}",
            },
            root=tmp_path,
        )
        assert report["status"] == status


def test_tool_selection_gate_requires_live_confirmation_and_hard_safety():
    from capslock.evaluation.tool_selection import promotion_gate

    values = dict(
        required_tools=100,
        recalled_tools=99,
        full_schema_tokens=[100, 100],
        selected_schema_tokens=[80, 70],
        hard_gate_failures=0,
        paired_resolve_ci95=(-0.02, 0.03),
        live_confirmed=True,
    )
    assert promotion_gate(**values)["eligible_for_promotion"] is True
    assert (
        promotion_gate(**{**values, "live_confirmed": False})["eligible_for_promotion"]
        is False
    )
    assert (
        promotion_gate(**{**values, "hard_gate_failures": 1})["eligible_for_promotion"]
        is False
    )
    assert (
        promotion_gate(**{**values, "recalled_tools": 98})["eligible_for_promotion"]
        is False
    )
    assert (
        promotion_gate(**{**values, "required_tools": 0, "recalled_tools": 0})[
            "eligible_for_promotion"
        ]
        is False
    )


def test_trusted_tool_selection_state_and_prefix_metadata_exclude_tool_content():
    from capslock.tooling.selection import SelectionState, prompt_metadata

    state = SelectionState("fix parser")
    state.observe(
        [("read_file", True), ("not_registered", False)], allowed_names={"read_file"}
    )
    assert "read_file" in state.query(planning=False)
    assert "not_registered" not in state.query(planning=False)
    first = prompt_metadata(
        [{"role": "system", "content": "stable"}, {"role": "user", "content": "goal"}],
        [],
    )
    second = prompt_metadata(
        [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "retrieved"},
        ],
        [],
    )
    assert first["core_prefix_sha256"] == second["core_prefix_sha256"]
    assert "stable" not in str(first) and "goal" not in str(first)


def test_external_intervention_count_and_older_payload_compatibility():
    from capslock.evaluation.external.runtime import _parse_outcome
    from capslock.evaluation.external.contracts import TaskResult, canonical_hash

    events = [
        {"event": "waiting_approval", "terminal": True, "data": {}},
        {"event": "waiting_input", "terminal": True, "data": {}},
        {"event": "completed", "terminal": True, "data": {}},
    ]
    outcome = _parse_outcome("\n".join(json.dumps(event) for event in events), 0, 1)
    assert outcome.human_interventions == 2
    payload = result("one", 1, True).payload()
    payload.pop("human_interventions", None)
    payload["result_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "result_hash"}
    )
    assert TaskResult.from_payload(payload).human_interventions is None
    task = ExternalTask("setupbench", "one", "task", "/tmp/task")
    report = build_report(
        [replace(result("one", 1, True), human_interventions=2)], [task]
    )
    assert report["human_interventions"] == 2
    assert (
        build_report([TaskResult.from_payload(payload)], [task])["human_interventions"]
        is None
    )
