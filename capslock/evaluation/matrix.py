"""Versioned experiment-matrix parsing and candidate expansion."""

from __future__ import annotations

import itertools
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .models import PolicyCandidate, canonical_hash
from .registry import METRICS, baseline_values


@dataclass(frozen=True)
class ExperimentMatrix:
    schema_version: int
    matrix_id: str
    task_set_version: str
    parameters: dict[str, tuple[int | float, ...]]
    subsystems: dict[str, tuple[str, ...]]
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0

    @property
    def fingerprint(self) -> str:
        return canonical_hash(
            {
                "schema_version": self.schema_version,
                "matrix_id": self.matrix_id,
                "task_set_version": self.task_set_version,
                "parameters": self.parameters,
                "subsystems": self.subsystems,
            }
        )[:16]

    def candidates(self, *, strategy: str = "oat") -> list[PolicyCandidate]:
        baseline = baseline_values()
        candidates = [PolicyCandidate("baseline", baseline)]
        seen = {candidates[0].fingerprint}
        if strategy == "oat":
            variants = (
                (f"{path}={value}", {**baseline, path: value})
                for path, values in self.parameters.items()
                for value in values
                if value != baseline.get(path)
            )
        elif strategy == "subsystem":
            variants_list: list[tuple[str, dict[str, int | float]]] = []
            for subsystem, paths in self.subsystems.items():
                values = [self.parameters[path] for path in paths]
                for combination in itertools.product(*values):
                    overrides = dict(zip(paths, combination, strict=True))
                    variants_list.append(
                        (
                            subsystem
                            + ":"
                            + ",".join(
                                f"{path}={value}" for path, value in overrides.items()
                            ),
                            {**baseline, **overrides},
                        )
                    )
            variants = iter(variants_list)
        else:
            raise ValueError("strategy must be oat or subsystem")
        for name, values in variants:
            candidate = PolicyCandidate(name, values)
            if candidate.fingerprint not in seen:
                seen.add(candidate.fingerprint)
                candidates.append(candidate)
        return candidates


def load_matrix(path: Path) -> ExperimentMatrix:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if int(document.get("schema_version", 0)) != 1:
        raise ValueError("evaluation matrix schema_version must be 1")
    raw_parameters = document.get("parameters")
    raw_subsystems = document.get("subsystems")
    if not isinstance(raw_parameters, dict) or not isinstance(raw_subsystems, dict):
        raise TypeError("evaluation matrix requires parameters and subsystems tables")
    registry = {item.path: item for item in METRICS}
    parameters: dict[str, tuple[int | float, ...]] = {}
    for path_name, raw_values in raw_parameters.items():
        if path_name not in registry:
            raise ValueError(f"unknown evaluated metric: {path_name}")
        if not isinstance(raw_values, list) or not raw_values:
            raise ValueError(f"{path_name} candidates must be a non-empty array")
        definition = registry[path_name]
        values = tuple(raw_values)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < definition.minimum
            or value > definition.maximum
            for value in values
        ):
            raise ValueError(f"{path_name} candidates exceed the registered range")
        parameters[path_name] = values
    subsystems = {
        str(name): tuple(str(path_name) for path_name in paths)
        for name, paths in raw_subsystems.items()
        if isinstance(paths, list)
    }
    assigned = {path_name for paths in subsystems.values() for path_name in paths}
    if assigned != set(parameters):
        raise ValueError(
            "every parameter must belong to exactly one declared subsystem"
        )
    pricing = document.get("pricing", {})
    return ExperimentMatrix(
        1,
        str(document.get("matrix_id", path.stem)),
        str(document.get("task_set_version", "core-v1")),
        parameters,
        subsystems,
        float(pricing.get("input_cost_per_million", 0))
        if isinstance(pricing, dict)
        else 0,
        float(pricing.get("output_cost_per_million", 0))
        if isinstance(pricing, dict)
        else 0,
    )
