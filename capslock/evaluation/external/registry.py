"""Load the external benchmark registry from versioned TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .contracts import ArtifactKind, ModelTrack, SuiteDefinition


@dataclass(frozen=True)
class ExternalDefaults:
    schema_version: int
    core_seed: int
    core_size_per_suite: int
    core_repetitions: int
    full_repetitions: int
    max_tool_rounds: int
    max_tool_calls: int
    max_tokens: int
    max_duration_seconds: int


@dataclass(frozen=True)
class ExternalRegistry:
    defaults: ExternalDefaults
    models: dict[str, ModelTrack]
    suites: dict[str, SuiteDefinition]

    def suite(self, suite_id: str) -> SuiteDefinition:
        try:
            return self.suites[suite_id]
        except KeyError as exc:
            raise ValueError(f"unknown external suite: {suite_id}") from exc

    def model(self, track: str) -> ModelTrack:
        try:
            return self.models[track]
        except KeyError as exc:
            raise ValueError(f"unknown model track: {track}") from exc


def load_registry(path: Path) -> ExternalRegistry:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if int(document.get("schema_version", 0)) != 1:
        raise ValueError("external registry schema_version must be 1")
    raw_defaults = dict(document.get("defaults") or {})
    defaults = ExternalDefaults(schema_version=1, **raw_defaults)
    models = {
        name: ModelTrack(name=name, **dict(values))
        for name, values in dict(document.get("models") or {}).items()
    }
    suites = {}
    for name, raw in dict(document.get("suites") or {}).items():
        values = dict(raw)
        values.setdefault("full_size", None)
        values["artifact_kind"] = ArtifactKind(values["artifact_kind"])
        for key in (
            "harness_probe",
            "dataset_ids",
            "excluded_splits",
            "resource_classes",
        ):
            if key in values:
                values[key] = tuple(str(item) for item in values[key])
        suites[name] = SuiteDefinition(id=name, **values)
    if set(models) != {"flash", "pro"}:
        raise ValueError("external registry must define flash and pro tracks")
    expected = {
        "swebench_verified",
        "swebench_live",
        "multi_swe_bench",
        "featurebench",
        "terminal_bench",
        "setupbench",
    }
    if set(suites) != expected:
        raise ValueError("external registry must define exactly the six v1 suites")
    return ExternalRegistry(defaults, models, suites)
