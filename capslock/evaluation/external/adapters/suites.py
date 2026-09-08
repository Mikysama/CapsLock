"""Six v1 adapters and their accepted official grader entry points."""

from __future__ import annotations

from .base import OfficialAdapter
from ..contracts import SuiteDefinition


class SweBenchVerifiedAdapter(OfficialAdapter):
    id = "swebench_verified"
    accepted_grade_prefixes = (("swebench", "eval", "verified"),)


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
