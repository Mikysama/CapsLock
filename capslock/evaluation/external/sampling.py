"""Deterministic, outcome-independent sampling for External Core."""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from collections.abc import Iterable

from .contracts import ExternalTask, canonical_hash


def _stable_key(task: ExternalTask, seed: int) -> str:
    value = f"{seed}:{task.suite}:{task.instance_id}".encode()
    return hashlib.sha256(value).hexdigest()


def _patch_quartiles(tasks: list[ExternalTask]) -> dict[str, int]:
    ordered = sorted(tasks, key=lambda item: (item.gold_patch_size, item.instance_id))
    count = max(1, len(ordered))
    return {
        item.instance_id: min(3, index * 4 // count)
        for index, item in enumerate(ordered)
    }


def select_core_tasks(
    tasks: list[ExternalTask], *, size: int, seed: int
) -> tuple[list[ExternalTask], str]:
    """Balance primary strata, repositories, and patch-size quartiles."""
    if size <= 0:
        raise ValueError("sample size must be positive")
    by_id = {task.instance_id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise ValueError("task instance IDs must be unique within a suite")
    if len(tasks) < size:
        raise ValueError(f"cannot select {size} tasks from a catalog of {len(tasks)}")
    suites = {task.suite for task in tasks}
    if len(suites) != 1:
        raise ValueError("core sampling accepts one suite at a time")

    quartiles = _patch_quartiles(tasks)
    buckets: dict[str, dict[str, dict[int, list[ExternalTask]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for task in tasks:
        primary = (
            task.language.casefold()
            if task.language and task.language.casefold() != "unknown"
            else task.task_type.casefold()
        )
        repository = task.repository or "unknown"
        buckets[primary][repository][quartiles[task.instance_id]].append(task)
    primary_queues: dict[str, deque[ExternalTask]] = {}
    for primary, repositories in sorted(buckets.items()):
        repository_queues: dict[str, deque[ExternalTask]] = {}
        for repository, patch_buckets in sorted(repositories.items()):
            quartile_queues = []
            for quartile, values in sorted(patch_buckets.items()):
                values.sort(key=lambda item: _stable_key(item, seed))
                quartile_queues.append((quartile, deque(values)))
            repository_queues[repository] = deque(_round_robin(quartile_queues))
        primary_queues[primary] = deque(_round_robin(repository_queues.items()))

    selected: list[ExternalTask] = []
    queues = list(sorted(primary_queues.items()))
    while len(selected) < size:
        remaining: list[tuple[str, deque[ExternalTask]]] = []
        for primary, queue in queues:
            if queue and len(selected) < size:
                selected.append(queue.popleft())
            if queue:
                remaining.append((primary, queue))
        if not remaining and len(selected) < size:
            raise RuntimeError("core sampling exhausted tasks unexpectedly")
        queues = remaining
    selected.sort(key=lambda item: _stable_key(item, seed))
    return selected, canonical_hash([item.instance_id for item in selected])


def _round_robin(
    queues: Iterable[tuple[object, Iterable[ExternalTask]]],
) -> list[ExternalTask]:
    active = [(key, deque(values)) for key, values in queues]
    selected = []
    while active:
        remaining = []
        for key, queue in active:
            if queue:
                selected.append(queue.popleft())
            if queue:
                remaining.append((key, queue))
        active = remaining
    return selected
