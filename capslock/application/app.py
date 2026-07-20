"""Async workspace composition root and resource ownership."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings
from ..layout import ProjectLayout
from ..memory import MemoryService
from ..observability import EventSink
from ..permissions import PermissionMode
from ..policy import WorkspacePolicy
from ..runtime import AsyncOpenAIChatModel, WorkspaceAgent
from ..skills import SkillRegistry, SkillService
from ..storage.memory_v2 import MemoryRepositories
from ..storage.repositories_v2 import WorkspaceRepositories
from .action_system import (
    ActionCoordinator,
    CommandActionHandler,
    FileActionHandler,
    McpActionHandler,
    WebActionHandler,
)
from .workflow import WorkflowService


class WorkspaceApplication:
    def __init__(
        self,
        *,
        workspace: Path,
        layout: ProjectLayout,
        settings: Settings,
        client: Any,
        repositories: WorkspaceRepositories,
        memory_repositories: MemoryRepositories,
        agent: WorkspaceAgent,
    ) -> None:
        self.workspace = workspace
        self.layout = layout
        self.settings = settings
        self.client = client
        self.repositories = repositories
        self.memory_repositories = memory_repositories
        self.agent = agent

    @classmethod
    async def open(
        cls,
        *,
        workspace: Path,
        settings: Settings,
        client: Any,
        session_id: str | None = None,
        layout: ProjectLayout | None = None,
    ) -> "WorkspaceApplication":
        root = workspace.resolve()
        layout = layout or ProjectLayout.discover(root)
        repositories = await WorkspaceRepositories.open(layout.database, workspace=root)
        memory_repositories = None
        try:
            memory_path = settings.memory.database or layout.user.canonical_memory
            memory_repositories = await MemoryRepositories.open(memory_path)
            if session_id is None:
                session = await repositories.sessions.create(
                    settings.model_config.model
                )
            else:
                session = await repositories.sessions.get(session_id)
                if session is None:
                    raise ValueError(f"session does not exist: {session_id}")
            stored_mode = await repositories.settings.workspace(
                "permission_mode", settings.permission_mode
            )
            permission_mode = PermissionMode.parse(
                stored_mode or settings.permission_mode
            )
            events = EventSink(layout.events)
            disabled = await repositories.settings.disabled_skills()
            skill_registry = SkillRegistry(
                root,
                disabled=lambda name: name in disabled,
                layout=layout,
            )
            skill_service = SkillService(skill_registry, events.emit)
            workflow = WorkflowService(repositories)
            policy = WorkspacePolicy(root)
            memory = MemoryService(
                memory_repositories,
                workspace=root,
                session_id=session.id,
                project_write_enabled=settings.memory.project_write_enabled,
                event=events.emit,
                source_validator=repositories.workflow.run_completed,
            )
            agent_ref: dict[str, WorkspaceAgent] = {}

            def actions(run_id: str) -> ActionCoordinator:
                handlers = [
                    FileActionHandler(policy),
                    CommandActionHandler(
                        policy,
                        timeout_seconds=settings.command.command_timeout_seconds,
                        output_limit_bytes=settings.command.command_output_bytes,
                    ),
                    WebActionHandler(
                        repositories,
                        tavily_api_key=settings.web.tavily_api_key,
                        timeout_seconds=settings.web.web_timeout_seconds,
                        max_bytes=settings.web.web_max_bytes,
                        max_redirects=settings.web.web_max_redirects,
                    ),
                    McpActionHandler(
                        policy,
                        timeout_seconds=settings.mcp.mcp_timeout_seconds,
                        output_limit_bytes=settings.mcp.mcp_output_bytes,
                        layout=layout,
                    ),
                ]
                return ActionCoordinator(
                    repositories,
                    session_id=session.id,
                    run_id=run_id,
                    handlers=handlers,
                    event=events.emit,
                    permission_mode=agent_ref["agent"].permission_mode
                    if "agent" in agent_ref
                    else permission_mode,
                )

            agent = WorkspaceAgent(
                workspace=root,
                model_name=settings.model_config.model,
                chat_model=AsyncOpenAIChatModel(client),
                repositories=repositories,
                workflow=workflow,
                session_id=session.id,
                policy=policy,
                action_factory=actions,
                skill_registry=skill_registry,
                skill_service=skill_service,
                events=events,
                memory=memory,
                permission_mode=permission_mode,
                max_turns=settings.runtime.max_turns,
                max_context_messages=settings.runtime.max_context_messages,
                input_cost_per_million=settings.model_config.input_cost_per_million,
                output_cost_per_million=settings.model_config.output_cost_per_million,
            )
            agent_ref["agent"] = agent
            return cls(
                workspace=root,
                layout=layout,
                settings=settings,
                client=client,
                repositories=repositories,
                memory_repositories=memory_repositories,
                agent=agent,
            )
        except Exception:
            if memory_repositories is not None:
                await memory_repositories.close()
            await repositories.close()
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
            raise

    async def close(self) -> None:
        try:
            close = getattr(self.client, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
        finally:
            try:
                await self.repositories.close()
            finally:
                await self.memory_repositories.close()

    async def __aenter__(self) -> "WorkspaceApplication":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()
