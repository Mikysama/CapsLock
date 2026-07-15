"""Dependencies shared by interactive CLI handlers."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console

from ..runtime import WorkspaceAgent


@dataclass(frozen=True)
class CliContext:
    console: Console
    agent: WorkspaceAgent
