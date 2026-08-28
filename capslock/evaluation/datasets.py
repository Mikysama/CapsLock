"""Reviewed synthetic task generation for the core-v1 benchmark."""

from __future__ import annotations

from .models import EvaluationTask

TUNING_COUNTS = {"runtime": 40, "context": 40, "loop": 40, "memory": 60, "agents": 40}
CONFIRM_COUNTS = {name: 12 for name in TUNING_COUNTS}


def _runtime(index: int, split: str) -> EvaluationTask:
    rounds = (2, 6, 10, 18, 26, 34, 46, 58)[index % 8]
    latency = (8, 14, 24, 38, 55, 75, 105)[index % 7]
    parallel = (1, 2, 4, 8, 12, 16)[index % 6]
    repairs = (0, 0, 1, 2)[index % 4]
    return EvaluationTask(
        f"{split}-runtime-{index:03d}",
        "runtime",
        split,
        f"Complete a {rounds}-round workspace workflow with {parallel} independent reads.",
        {
            "rounds": rounds,
            "provider_latency": latency,
            "parallel_reads": parallel,
            "repair_attempts": repairs,
        },
        critical=index % 10 == 0,
    )


def _context(index: int, split: str) -> EvaluationTask:
    positions = ("front", "middle", "tail")
    kinds = ("transcript", "compaction", "tool_result", "artifact")
    return EvaluationTask(
        f"{split}-context-{index:03d}",
        "context",
        split,
        f"Recover the canary from {kinds[index % 4]} at the {positions[index % 3]} position.",
        {
            "position": positions[index % 3],
            "kind": kinds[index % 4],
            "pressure": (0.72, 0.78, 0.82, 0.87, 0.92)[index % 5],
            "required_turns": (2, 4, 5, 6)[index % 4],
            "required_tokens": (8_192, 16_384, 24_000, 32_000)[index % 4],
            "compaction_failures": index % 3,
        },
        critical=index % 8 == 0,
    )


def _loop(index: int, split: str) -> EvaluationTask:
    true_loop = index % 2 == 0
    kind = ("repeat", "failure", "cycle")[index % 3]
    repetitions = (2, 3, 4, 5, 6)[index % 5]
    return EvaluationTask(
        f"{split}-loop-{index:03d}",
        "loop",
        split,
        ("Stop a non-progressing" if true_loop else "Allow a legitimate")
        + f" {kind} sequence.",
        {
            "true_loop": true_loop,
            "kind": kind,
            "repetitions": repetitions,
            "cycle_length": (2, 4, 6)[index % 3],
            "labelled_sequences": 50,
        },
        critical=true_loop and index % 10 == 0,
    )


def _memory(index: int, split: str) -> EvaluationTask:
    unsafe = index % 10 in {8, 9}
    relevant = index % 10 < 7
    return EvaluationTask(
        f"{split}-memory-{index:03d}",
        "memory",
        split,
        "Recall the relevant reviewed fact without selecting stale, isolated, or injected content.",
        {
            "rank": (1, 2, 3, 5, 7, 9)[index % 6],
            "bytes": (512, 1024, 2048, 4096, 6144)[index % 5],
            "semantic_score": (0.30, 0.40, 0.46, 0.52, 0.62)[index % 5],
            "retrieval": (0.30, 0.40, 0.50, 0.62, 0.75)[index % 5],
            "scope": (0.70, 0.85, 1.0)[index % 3],
            "confidence": (0.60, 0.75, 0.90, 1.0)[index % 4],
            "freshness": (0.20, 0.50, 0.80, 1.0)[index % 4],
            "source_validity": 0.25 if unsafe else 1.0,
            "calibrated_probability": 0.99 if relevant else 0.01,
            "relevant": relevant,
            "unsafe": unsafe,
            "labelled_memories": 20,
        },
        critical=unsafe,
    )


def _agents(index: int, split: str) -> EvaluationTask:
    conflicting = index % 10 in {8, 9}
    return EvaluationTask(
        f"{split}-agents-{index:03d}",
        "agents",
        split,
        "Delegate independent work and merge verified child results.",
        {
            "children": (1, 2, 4, 6, 8)[index % 5],
            "concurrency": (1, 2, 3, 4)[index % 4],
            "child_rounds": (4, 8, 12, 18, 22)[index % 5],
            "conflicting": conflicting,
        },
        critical=conflicting,
    )


_BUILDERS = {
    "runtime": _runtime,
    "context": _context,
    "loop": _loop,
    "memory": _memory,
    "agents": _agents,
}


def build_tasks(*, split: str) -> tuple[EvaluationTask, ...]:
    if split not in {"tune", "confirm"}:
        raise ValueError("task split must be tune or confirm")
    counts = TUNING_COUNTS if split == "tune" else CONFIRM_COUNTS
    return tuple(
        _BUILDERS[subsystem](index, split)
        for subsystem, count in counts.items()
        for index in range(count)
    )
