#!/usr/bin/env python3
"""Adapt CapsLock patch artifacts to the official SWE-bench prediction format."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    executable = os.environ.get("CAPSLOCK_EVAL_SWEBENCH_BIN", "swebench-real")
    predictions_index = _option_index(argv, "--predictions")
    instance_index = _option_index(argv, "--instance")
    if predictions_index is None or instance_index is None:
        return subprocess.run([executable, *argv], check=False).returncode
    if predictions_index + 1 >= len(argv) or instance_index + 1 >= len(argv):
        raise SystemExit("SWE-bench bridge received an incomplete prediction command")
    artifact = Path(argv[predictions_index + 1]).resolve()
    # The production adapter already owns JSONL conversion, cleanup and run IDs.
    # Keep this legacy wrapper composable without wrapping JSON inside model_patch.
    if artifact.suffix == ".jsonl":
        return subprocess.run([executable, *argv], check=False).returncode
    instance_id = argv[instance_index + 1]
    prediction = artifact.with_name(f"{artifact.name}.prediction.jsonl")
    payload = {
        "instance_id": instance_id,
        "model_name_or_path": "capslock",
        "model_patch": artifact.read_text(encoding="utf-8", errors="replace"),
    }
    prediction.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    command = list(argv)
    command[predictions_index + 1] = str(prediction)
    run_id_index = _option_index(command, "--run-id")
    if run_id_index is not None and run_id_index + 1 < len(command):
        command[run_id_index + 1] = _repetition_run_id(
            command[run_id_index + 1], artifact
        )
    try:
        return subprocess.run([executable, *command], check=False).returncode
    finally:
        prediction.unlink(missing_ok=True)


def _option_index(argv: list[str], option: str) -> int | None:
    try:
        return argv.index(option)
    except ValueError:
        return None


def _repetition_run_id(base_run_id: str, artifact: Path) -> str:
    """Keep SWE-bench's cached grading result unique per task repetition."""
    # External runner stores artifacts at tasks/<instance_id>/<ordinal>/...;
    # retaining that ordinal makes retries/resumes deterministic.
    ordinal = artifact.parent.name
    if not ordinal.isdigit():
        return base_run_id
    suffix = f"-rep{int(ordinal)}"
    return base_run_id if base_run_id.endswith(suffix) else base_run_id + suffix


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
