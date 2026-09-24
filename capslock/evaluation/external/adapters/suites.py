"""Six v1 adapters and their accepted official grader entry points."""

from __future__ import annotations

import json
from pathlib import Path

from .base import AdapterContext, Artifact, OfficialAdapter
from ..contracts import ArtifactKind, ExternalTask, SuiteDefinition


class SweBenchVerifiedAdapter(OfficialAdapter):
    id = "swebench_verified"
    accepted_grade_prefixes = (("swebench", "eval", "verified"),)

    def _prepare_grade_command(
        self,
        command: list[str],
        task: ExternalTask,
        artifact: Artifact,
        context: AdapterContext,
    ) -> tuple[list[str], tuple[Path, ...]]:
        if artifact.kind is not ArtifactKind.GIT_PATCH:
            return command, ()
        try:
            predictions_index = command.index("--predictions")
        except ValueError:
            return command, ()
        if predictions_index + 1 >= len(command):
            raise ValueError(
                "SWE-bench grader command has an incomplete --predictions option"
            )
        prepared = list(command)
        run_id = context.run_id
        try:
            run_index = prepared.index("--run-id")
        except ValueError:
            run_index = len(prepared)
            prepared.extend(("--run-id", run_id))
        else:
            if run_index + 1 >= len(prepared):
                raise ValueError(
                    "SWE-bench grader command has an incomplete --run-id option"
                )
            run_id = prepared[run_index + 1]
        suffix = f"-rep{context.ordinal}"
        prepared[run_index + 1] = run_id if run_id.endswith(suffix) else run_id + suffix
        prediction = artifact.path.with_name(f"{artifact.path.stem}.prediction.jsonl")
        prediction.write_text(
            json.dumps(
                {
                    "instance_id": task.instance_id,
                    "model_name_or_path": "capslock",
                    "model_patch": artifact.path.read_text(
                        encoding="utf-8", errors="replace"
                    ),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        prepared[predictions_index + 1] = str(prediction)
        return prepared, (prediction,)


class SweBenchLiveAdapter(OfficialAdapter):
    id = "swebench_live"
    accepted_grade_prefixes = (
        ("python", "-m", "evaluation.evaluation"),
        ("python3", "-m", "evaluation.evaluation"),
    )


class MultiSweBenchAdapter(OfficialAdapter):
    id = "multi_swe_bench"
    accepted_grade_prefixes = (
        ("python", "-m", "multi_swe_bench.harness.run_evaluation"),
        ("python3", "-m", "multi_swe_bench.harness.run_evaluation"),
    )


class FeatureBenchAdapter(OfficialAdapter):
    id = "featurebench"
    accepted_grade_prefixes = (("fb", "eval"),)


class TerminalBenchAdapter(OfficialAdapter):
    id = "terminal_bench"
    accepted_grade_prefixes = (("harbor", "run"), ("uv", "run", "harbor", "run"))


class SetupBenchAdapter(OfficialAdapter):
    id = "setupbench"
    accepted_grade_prefixes = (
        ("python", "setupbench/evaluation_harness.py"),
        ("python3", "setupbench/evaluation_harness.py"),
    )


ADAPTERS: dict[str, type[OfficialAdapter]] = {
    item.id: item
    for item in (
        SweBenchVerifiedAdapter,
        SweBenchLiveAdapter,
        MultiSweBenchAdapter,
        FeatureBenchAdapter,
        TerminalBenchAdapter,
        SetupBenchAdapter,
    )
}


def adapter_for(definition: SuiteDefinition) -> OfficialAdapter:
    try:
        adapter = ADAPTERS[definition.adapter]
    except KeyError as exc:
        raise ValueError(f"unknown official adapter: {definition.adapter}") from exc
    return adapter(definition)
