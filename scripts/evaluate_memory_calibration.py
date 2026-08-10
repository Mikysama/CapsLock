#!/usr/bin/env python3
"""Check the bundled memory-verifier calibration and automatic adoption gates."""

from __future__ import annotations

import json
from pathlib import Path

from capslock.memory.candidates import _calibrated_probability


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "memory_candidate_calibration.jsonl"
)


def evaluate() -> dict[str, float | int]:
    cases = [
        json.loads(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    decisions = []
    calibration_error = 0.0
    for case in cases:
        probability = _calibrated_probability(float(case["raw_score"]))
        threshold = 0.95 if int(case["sources"]) == 1 else 0.98
        adopt = (
            probability >= threshold
            and bool(case["supported"])
            and not bool(case["instruction_like"])
        )
        decisions.append((case, adopt))
        calibration_error += abs(probability - float(bool(case["supported"])))
    eligible = [item for item in decisions if item[0]["supported"] and not item[0]["instruction_like"]]
    adopted = [item for item in decisions if item[1]]
    true_adopted = [item for item in adopted if item[0]["supported"] and not item[0]["instruction_like"]]
    return {
        "cases": len(cases),
        "automatic_precision": len(true_adopted) / len(adopted) if adopted else 1.0,
        "supported_recall": len(true_adopted) / len(eligible) if eligible else 1.0,
        "ece": calibration_error / len(cases),
    }


def main() -> int:
    result = evaluate()
    print(json.dumps(result, indent=2))
    return 0 if result["automatic_precision"] >= 0.98 and result["supported_recall"] >= 0.90 and result["ece"] <= 0.05 else 1


if __name__ == "__main__":
    raise SystemExit(main())
