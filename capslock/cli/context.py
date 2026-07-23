"""Dependencies shared by interactive CLI handlers."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console

from ..application.queries import WorkspaceQueries
from ..runtime import AgentSession


@dataclass(frozen=True)
class CliContext:
    console: Console
    session: AgentSession
    queries: WorkspaceQueries | None = None

    def __post_init__(self) -> None:
        if self.queries is None:
            object.__setattr__(self, "queries", getattr(self.session, "queries", None))

    def require_queries(self) -> WorkspaceQueries:
        if self.queries is None:
            raise RuntimeError("workspace queries are unavailable")
        return self.queries
