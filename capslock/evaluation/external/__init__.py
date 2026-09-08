"""Versioned external benchmark orchestration for CapsLock."""

from .contracts import (
    ArtifactKind,
    ExternalTask,
    ModelTrack,
    RunManifest,
    SuiteDefinition,
    TaskResult,
    canonical_hash,
)
from .registry import ExternalRegistry, load_registry

__all__ = [
    "ArtifactKind",
    "ExternalRegistry",
    "ExternalTask",
    "ModelTrack",
    "RunManifest",
    "SuiteDefinition",
    "TaskResult",
    "canonical_hash",
    "load_registry",
]
