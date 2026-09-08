"""Normalised task-catalog loading and agent-data isolation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import ExternalTask, reject_hidden_task_data
from .io import read_jsonl


def load_catalog(path: Path, *, suite: str | None = None) -> list[ExternalTask]:
    tasks = [_task(row, path=path) for row in read_jsonl(path)]
    if suite is not None and any(task.suite != suite for task in tasks):
        raise ValueError(f"catalog contains tasks outside suite {suite}: {path}")
    identifiers = [task.instance_id for task in tasks]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"catalog has duplicate instance IDs: {path}")
    return tasks


def _task(row: dict[str, Any], *, path: Path) -> ExternalTask:
    allowed = {
        "suite",
        "instance_id",
        "problem_statement",
        "workspace_source",
        "repository",
        "language",
        "task_type",
        "gold_patch_size",
        "resource_class",
        "grader",
    }
    unknown = sorted(set(row) - allowed)
    if unknown:
        raise ValueError(f"unknown catalog fields in {path}: {', '.join(unknown)}")
    reject_hidden_task_data(
        {key: value for key, value in row.items() if key != "grader"}
    )
    grader = row.get("grader") or {}
    if not isinstance(grader, dict):
        raise ValueError(f"grader must be an object in {path}")
    return ExternalTask(
        suite=str(row.get("suite", "")),
        instance_id=str(row.get("instance_id", "")),
        problem_statement=str(row.get("problem_statement", "")),
        workspace_source=str(row.get("workspace_source", "")),
        repository=str(row.get("repository", "")),
        language=str(row.get("language", "unknown")),
        task_type=str(row.get("task_type", "unknown")),
        gold_patch_size=int(row.get("gold_patch_size", 0)),
        resource_class=str(row.get("resource_class", "linux-amd64-cpu")),
        grader=grader,
    )
