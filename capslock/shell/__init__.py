"""Shell assessment, classification, sandboxing and process management."""

from .assessment import ShellAssessment, assess_shell
from .classifier import ModelShellClassifier, ShellClassification
from .processes import ProcessJob, SessionProcessManager, stop_process
from .parser import ShellSegment, ShellSyntax, TreeSitterShellParser, parse_shell
from .sandbox import SandboxedCommand, ShellSandboxUnavailable, sandboxed_command

__all__ = [
    "ModelShellClassifier",
    "ProcessJob",
    "SandboxedCommand",
    "SessionProcessManager",
    "ShellAssessment",
    "ShellClassification",
    "ShellSandboxUnavailable",
    "ShellSegment",
    "ShellSyntax",
    "TreeSitterShellParser",
    "assess_shell",
    "parse_shell",
    "sandboxed_command",
    "stop_process",
]
