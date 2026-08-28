"""Reproducible behavioural-policy evaluation."""

from .datasets import build_tasks
from .matrix import ExperimentMatrix, load_matrix
from .models import EvaluationTask, PolicyCandidate, SampleResult
from .optimizer import propose_memory_weights
from .runner import EvaluationRunner
from .selection import analyse_candidates

__all__ = [
    "EvaluationRunner",
    "EvaluationTask",
    "ExperimentMatrix",
    "PolicyCandidate",
    "SampleResult",
    "analyse_candidates",
    "build_tasks",
    "load_matrix",
    "propose_memory_weights",
]
