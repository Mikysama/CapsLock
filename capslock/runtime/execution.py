"""Foreground run execution boundary used by the agent facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import WorkspaceAgent


class RunExecutionService:
    """Own execution dispatch while WorkspaceAgent owns active-run lifecycle."""

    def __init__(self, agent: "WorkspaceAgent") -> None:
        self.agent = agent

    async def execute(self, question: str, **options: Any) -> None:
        await self.agent._run_execution(question, **options)
