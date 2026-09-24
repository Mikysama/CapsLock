"""Fixed, evaluation-only context compaction candidates."""

from __future__ import annotations

from ..runtime.context import ContextEvaluationPolicy
from .models import PolicyCandidate
from .registry import baseline_values


def context_compaction_candidates() -> list[PolicyCandidate]:
    baseline = baseline_values()

    def candidate(name: str, **overrides: int | float) -> PolicyCandidate:
        return PolicyCandidate(name, {**baseline, **overrides})

    return [
        candidate("baseline"),
        candidate(
            "trigger-85",
            **{
                "context.trigger_ratio": 0.85,
                "context.minimum_headroom_tokens": 16_384,
            },
        ),
        candidate(
            "trigger-90",
            **{
                "context.trigger_ratio": 0.90,
                "context.minimum_headroom_tokens": 16_384,
            },
        ),
        candidate("dynamic-recent", **{"context.dynamic_recent": 1}),
        candidate("protected-tools", **{"context.protected_tools": 1}),
        candidate("exact-anchors", **{"context.exact_anchors": 1}),
        candidate(
            "combined",
            **{
                "context.trigger_ratio": 0.85,
                "context.minimum_headroom_tokens": 16_384,
                "context.dynamic_recent": 1,
                "context.protected_tools": 1,
                "context.exact_anchors": 1,
            },
        ),
    ]


def policy_for_candidate(candidate: PolicyCandidate) -> ContextEvaluationPolicy:
    values = candidate.values
    protected = bool(values.get("context.protected_tools", 0))
    return ContextEvaluationPolicy(
        minimum_headroom_tokens=int(values.get("context.minimum_headroom_tokens", 0)),
        dynamic_recent=bool(values.get("context.dynamic_recent", 0)),
        protect_latest_tool_round=protected,
        minimum_tool_reclaim_tokens=4_096 if protected else 0,
        exact_anchors=bool(values.get("context.exact_anchors", 0)),
    )


__all__ = ["context_compaction_candidates", "policy_for_candidate"]
