"""Reproducible behavioural-policy evaluation."""

from .datasets import build_tasks
from .matrix import ExperimentMatrix, load_matrix
from .models import EvaluationTask, PolicyCandidate, SampleResult
from .optimizer import propose_memory_weights
from .runner import EvaluationRunner
from .selection import analyse_candidates
from .context_compaction import context_compaction_candidates
from .long_context import load_long_context_tasks

__all__ = [
    "EvaluationRunner",
    "EvaluationTask",
    "ExperimentMatrix",
    "PolicyCandidate",
    "SampleResult",
    "analyse_candidates",
    "build_tasks",
    "context_compaction_candidates",
    "load_matrix",
    "load_long_context_tasks",
    "propose_memory_weights",
]
