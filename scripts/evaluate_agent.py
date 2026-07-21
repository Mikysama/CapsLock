#!/usr/bin/env python3
"""Run the versioned CapsLock core evaluation suite and emit a JSON report."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "agent_eval_v2.json"
BASELINE = ROOT / "tests" / "fixtures" / "agent_eval_baseline_v1.json"


def deterministic(output: Path | None) -> int:
    scenarios = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]
    with tempfile.TemporaryDirectory(prefix="capslock-eval-") as temporary:
        report_path = Path(temporary) / "junit.xml"
        command = [
            os.sys.executable,
            "-m",
            "pytest",
            "-q",
            *[item["test"] for item in scenarios],
            f"--junitxml={report_path}",
        ]
        started = time.monotonic()
        completed = subprocess.run(
            command, cwd=ROOT, check=False, capture_output=True, text=True
        )
        duration = time.monotonic() - started
        if not report_path.exists():
            report = {
                "schema_version": 2,
                "mode": "deterministic",
                "status": "failed",
                "scenario_count": len(scenarios),
                "passed": 0,
                "quality": 0,
                "duration_seconds": round(duration, 4),
                "cost_usd": 0,
                "categories": [item["category"] for item in scenarios],
                "regressions": ["test_runner"],
                "error": (
                    "deterministic evaluation did not produce JUnit output; "
                    f"pytest exited with {completed.returncode}: "
                    f"{(completed.stderr or completed.stdout).strip()[-1000:]}"
                ),
            }
            serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            if output:
                output.write_text(serialized, encoding="utf-8")
            print(serialized, end="")
            return 1
        root = ET.parse(report_path).getroot()
        cases = list(root.iter("testcase"))
        failures = sum(
            any(child.tag in {"failure", "error"} for child in case) for case in cases
        )
        durations = sorted(float(case.attrib.get("time", 0)) for case in cases)
    quality = 0 if not cases else (len(cases) - failures) / len(cases)
    p95 = (
        durations[min(len(durations) - 1, int(len(durations) * 0.95))]
        if durations
        else 0
    )
    report = {
        "schema_version": 2,
        "mode": "deterministic",
        "scenario_count": len(scenarios),
        "passed": len(cases) - failures,
        "quality": quality,
        "duration_seconds": round(duration, 4),
        "p95_case_latency_seconds": round(p95, 4),
        "cost_usd": 0,
        "categories": [item["category"] for item in scenarios],
    }
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    regressions = []
    if quality < float(baseline["minimum_quality"]):
        regressions.append("quality")
    if report["cost_usd"] > float(baseline["maximum_cost_usd"]):
        regressions.append("cost")
    if report["p95_case_latency_seconds"] > float(
        baseline["maximum_p95_case_latency_seconds"]
    ):
        regressions.append("latency")
    report["regressions"] = regressions
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 1 if completed.returncode or regressions else 0


async def live(
    provider: str,
    model: str,
    output: Path | None,
    input_cost_per_million: float,
    output_cost_per_million: float,
) -> int:
    from openai import AsyncOpenAI

    key_name = f"{provider.upper()}_API_KEY"
    api_key = os.environ.get(key_name) or os.environ.get("CAPSLOCK_API_KEY")
    base_url = os.environ.get(f"{provider.upper()}_BASE_URL") or os.environ.get(
        "CAPSLOCK_BASE_URL"
    )
    if not api_key:
        report = {
            "schema_version": 1,
            "mode": "live",
            "provider": provider,
            "model": model,
            "status": "skipped",
            "reason": f"missing {key_name} or CAPSLOCK_API_KEY",
        }
        serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if output:
            output.write_text(serialized, encoding="utf-8")
        print(serialized, end="")
        return 0
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    samples = []
    try:
        for item in fixture["live_samples"]:
            started = time.monotonic()
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": item["prompt"]}],
                max_tokens=128,
            )
            text = response.choices[0].message.content or ""
            usage = response.usage
            samples.append(
                {
                    "category": item["category"],
                    "passed": all(
                        token.casefold() in text.casefold()
                        for token in item.get("required", [])
                    ),
                    "latency_seconds": round(time.monotonic() - started, 4),
                    "input_tokens": int(usage.prompt_tokens if usage else 0),
                    "output_tokens": int(usage.completion_tokens if usage else 0),
                }
            )
    finally:
        await client.close()
    report = {
        "schema_version": 1,
        "mode": "live",
        "provider": provider,
        "model": model,
        "status": "sampled",
        "quality": sum(item["passed"] for item in samples) / len(samples),
        "cost_usd": sum(
            item["input_tokens"] * input_cost_per_million
            + item["output_tokens"] * output_cost_per_million
            for item in samples
        )
        / 1_000_000,
        "samples": samples,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("deterministic", "live"), default="deterministic"
    )
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input-cost-per-million", type=float, default=0)
    parser.add_argument("--output-cost-per-million", type=float, default=0)
    args = parser.parse_args()
    if args.mode == "live":
        return asyncio.run(
            live(
                args.provider,
                args.model,
                args.output,
                args.input_cost_per_million,
                args.output_cost_per_million,
            )
        )
    return deterministic(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
