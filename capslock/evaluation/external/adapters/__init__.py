"""Official-harness adapter registry."""

from .base import AdapterContext, Artifact, GradeOutcome, OfficialAdapter
from .suites import adapter_for

__all__ = [
    "AdapterContext",
    "Artifact",
    "GradeOutcome",
    "OfficialAdapter",
    "adapter_for",
]
