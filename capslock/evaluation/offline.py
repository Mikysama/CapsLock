"""Executable, offline kernel regression catalog; not a model benchmark."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from .external.contracts import canonical_hash
from .statistics import percentile


def load_manifest(path: Path, *, root: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        document.get("schema_version") != 1
        or document.get("kind") != "offline_kernel_regression"
    ):
        raise ValueError("unsupported offline regression manifest")
    identities, nodeids = set(), set()
    for item in document.get("scenarios", []):
        nodeid = item["nodeid"]
        relative, separator, function = nodeid.partition("::")
        target = (root / relative).resolve()
        if (
            not separator
            or not target.is_relative_to((root / "tests").resolve())
            or not target.is_file()
        ):
            raise ValueError(f"invalid test path: {relative}")
        functions = {
            node.name
            for node in ast.parse(target.read_text()).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if function not in functions:
            raise ValueError(f"test does not exist: {nodeid}")
        if item["id"] in identities or nodeid in nodeids:
            raise ValueError("duplicate scenario id or test nodeid")
        if item.get("grader") != "pytest_assertions" or not all(
            item.get(key) for key in ("group", "fixtures", "input", "expected")
        ):
            raise ValueError(
                "scenario requires fixtures, input, expected and pytest grader"
            )
        identities.add(item["id"])
        nodeids.add(nodeid)
    if (
        len(identities) != 60
        or sorted(Counter(item["group"] for item in document["scenarios"]).values())
        != [12] * 5
    ):
        raise ValueError("offline v1 requires five groups of twelve distinct scenarios")
    return document


def run_scenario(item: dict, *, root: Path, timeout: float = 180) -> dict:
    """Run a scenario in a fresh process and temporary fixture directory."""
    started = time.monotonic()
    with TemporaryDirectory(prefix="capslock-offline-") as temporary:
        report = Path(temporary) / "junit.xml"
        environment = dict(os.environ)
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        environment.pop("PYTEST_ADDOPTS", None)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "capslock.evaluation.offline_guard",
            item["nodeid"],
            f"--junitxml={report}",
            f"--basetemp={temporary}/fixtures",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = (completed.stdout + completed.stderr)[-16000:]
            if not report.is_file() or completed.returncode not in (0, 1):
                status = "infrastructure_error"
            else:
                cases = list(ET.parse(report).getroot().iter("testcase"))
                if not cases or any(
                    case.find("skipped") is not None or case.find("error") is not None
                    for case in cases
                ):
                    status = "infrastructure_error"
                else:
                    status = "passed" if completed.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            status, output = "infrastructure_error", "scenario exceeded timeout"
    return {
        "id": item["id"],
        "group": item["group"],
        "nodeid": item["nodeid"],
        "status": status,
        "duration_seconds": time.monotonic() - started,
        "output": output,
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=root / "evaluations/offline-regression-v1.json"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--repetitions", type=int, choices=(1, 3), default=1)
    args = parser.parse_args(argv)
    document = load_manifest(args.manifest, root=root)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "kind": document["kind"],
                    "scenario_count": len(document["scenarios"]),
                    "provider_calls": 0,
                    "groups": dict(
                        Counter(item["group"] for item in document["scenarios"])
                    ),
                },
                indent=2,
            )
        )
        return 0
    if args.output is None:
        parser.error("--output is required unless --dry-run is used")
    fingerprint = canonical_hash(
        {
            "manifest": document,
            "sources": {
                str(path.relative_to(root)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for folder in (root / "capslock", root / "tests")
                for path in sorted(folder.rglob("*.py"))
            },
            "repetitions": args.repetitions,
        }
    )
    results: list[dict] = []
    if args.resume and args.output.exists():
        prior = json.loads(args.output.read_text())
        if prior.get("fingerprint") != fingerprint:
            raise ValueError(
                "resume fingerprint differs; manifest, source or repetitions changed"
            )
        results = prior["results"]
    done = {(item["id"], item["repetition"]) for item in results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {}
    for item in document["scenarios"]:
        for ordinal in range(1, args.repetitions + 1):
            if (item["id"], ordinal) not in done:
                results.append({**run_scenario(item, root=root), "repetition": ordinal})
            passed = sum(value["status"] == "passed" for value in results)
            report = {
                "schema_version": 1,
                "kind": "offline_kernel_regression",
                "not_a_real_model_resolve_rate": True,
                "provider_calls": 0,
                "fingerprint": fingerprint,
                "scenario_count": 60,
                "attempt_count": len(results),
                "passed": passed,
                "failed": sum(value["status"] == "failed" for value in results),
                "infrastructure_errors": sum(
                    value["status"] == "infrastructure_error" for value in results
                ),
                "kernel_regression_pass_rate": passed / len(results),
                "p95_duration_seconds": percentile(
                    [value["duration_seconds"] for value in results], 0.95
                ),
                "reliable_success_3": (
                    sum(
                        all(
                            value["status"] == "passed"
                            for value in results
                            if value["id"] == scenario["id"]
                        )
                        for scenario in document["scenarios"]
                    )
                    / 60
                    if args.repetitions == 3 and len(results) == 180
                    else None
                ),
                "results": results,
            }
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
            temporary.replace(args.output)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "results"}, indent=2
        )
    )
    return 0 if report["passed"] == 60 * args.repetitions else 1


if __name__ == "__main__":
    raise SystemExit(main())
