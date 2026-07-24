"""Child-Agent collaboration assembly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..collaboration import (
    AgentOutputVerifier,
    AgentWorkspaceManager,
    ChildAgentRunner,
    CollaborationService,
)
from ..configuration import Settings
from ..interaction import RunInteraction
from ..plugins import PluginRegistry


def build_collaboration(
    *,
    settings: Settings,
    child_mode: bool,
    active_root: Path,
    state_root: Path,
    client: Any,
    plugins: PluginRegistry,
    interaction: RunInteraction,
    repository: Any,
    open_application: Any,
) -> CollaborationService | None:
    if child_mode or not settings.agents.enabled:
        return None
    manager = AgentWorkspaceManager(active_root, state_root=state_root)
    runner = ChildAgentRunner(
        settings=settings,
        client=client,
        plugin_registry=plugins,
        interaction=interaction,
        repository=repository,
        open_application=open_application,
    )
    service = CollaborationService(
        workspace_manager=manager,
        repository=repository,
        max_children=settings.agents.max_children,
        max_concurrency=settings.agents.max_concurrency,
        max_depth=settings.agents.max_depth,
        child_runner=runner,
        verifier=AgentOutputVerifier(),
        background_enabled=settings.agents.background_enabled,
    )
    runner.collaboration = service
    return service


__all__ = ["build_collaboration"]
