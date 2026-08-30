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
from ..workspace_writes import WorkspaceMutationCoordinator


def build_collaboration(
    *,
    settings: Settings,
    child_mode: bool,
    active_root: Path,
    state_root: Path,
    client: Any,
    plugins: PluginRegistry,
    mcp: Any | None = None,
    interaction: RunInteraction,
    repository: Any,
    action_repository: Any | None = None,
    open_application: Any,
    memory: Any = None,
    write_coordinator: WorkspaceMutationCoordinator | None = None,
) -> CollaborationService | None:
    if child_mode or not settings.agents.enabled:
        return None
    manager = AgentWorkspaceManager(
        active_root,
        state_root=state_root,
        write_coordinator=write_coordinator,
    )
    runner = ChildAgentRunner(
        settings=settings,
        client=client,
        plugin_registry=plugins,
        mcp_manager=mcp,
        interaction=interaction,
        repository=repository,
        action_repository=action_repository,
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
        proposal_handler=(
            memory.promote_agent_proposals if memory is not None else None
        ),
        mailbox_enabled=settings.agents.mailbox_enabled,
        message_ttl_seconds=settings.agents.message_ttl_seconds,
        default_workspace_mode=settings.agents.default_workspace_mode,
    )
    runner.collaboration = service
    if memory is not None:
        runner.memory_loader = memory.agent_memories
    return service


__all__ = ["build_collaboration"]
