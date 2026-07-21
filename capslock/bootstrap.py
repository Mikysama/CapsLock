"""Concrete workspace composition root and resource ownership."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .application.action_system import (
    ActionCoordinator,
    CommandActionHandler,
    FileActionHandler,
    McpActionHandler,
    WebActionHandler,
)
from .application.workflow import WorkflowService
from .config import Settings
from .interaction import RunInteraction
from .layout import ProjectLayout
from .memory import MemoryService
from .memory.embeddings import ExternalEmbeddingConfig
from .observability import EventSink
from .permissions import PermissionMode
from .policy import WorkspacePolicy
from .runtime import AsyncOpenAIChatModel, ModelRouter, WorkspaceAgent
from .skills import SkillRegistry, SkillService
from .storage.memory_v2 import MemoryRepositories
from .storage.repositories_v2 import WorkspaceRepositories


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
            interaction = RunInteraction(permission_mode=permission_mode)
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
            raw_clients = client if isinstance(client, dict) else {"default": client}
            adapters = {
                name: AsyncOpenAIChatModel(
                    item,
                    max_output_tokens={
                        profile.model: profile.max_output_tokens
                        for profile in (settings.models or {}).values()
                        if profile.provider == name
                    },
                )
                for name, item in raw_clients.items()
            }
            router = ModelRouter(
                providers=settings.providers or {},
                profiles=settings.models or {},
                routing=settings.routing,
                clients=adapters,
                audit=repositories.models,
                budget=settings.budget,
            )
            external_embedding_profiles = {}
            for profile_name in settings.routing.embedding if settings.routing else ():
                profile = (settings.models or {})[profile_name]
                provider = (settings.providers or {})[profile.provider]
                provider_client = raw_clients.get(profile.provider)
                if provider_client is not None:
                    external_embedding_profiles[profile_name] = ExternalEmbeddingConfig(
                        profile_name,
                        provider.name,
                        profile.model,
                        provider.data_policy,
                        profile.input_cost_per_million,
                        provider_client,
                    )
            memory = MemoryService(
                memory_repositories,
                workspace=root,
                session_id=session.id,
                project_write_enabled=settings.memory.project_write_enabled,
                event=events.emit,
                source_validator=repositories.workflow.run_completed,
                external_embedding_profiles=external_embedding_profiles,
            )

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
                    interaction=interaction,
                )

            agent = WorkspaceAgent(
                workspace=root,
                model_name=settings.model_config.model,
                chat_model=router,
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
                max_tool_rounds=settings.runtime.max_tool_rounds,
                max_context_messages=settings.runtime.max_context_messages,
                input_cost_per_million=settings.model_config.input_cost_per_million,
                output_cost_per_million=settings.model_config.output_cost_per_million,
                max_run_tokens=settings.budget.max_run_tokens,
                max_run_usd=settings.budget.max_run_usd,
                loop_detection=settings.loop_detection,
                interaction=interaction,
            )
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
            await _close_clients(client)
            raise

    async def close(self) -> None:
        try:
            await _close_clients(self.client)
        finally:
            try:
                await self.repositories.close()
            finally:
                await self.memory_repositories.close()

    async def __aenter__(self) -> "WorkspaceApplication":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()


async def _close_clients(clients: Any) -> None:
    values = clients.values() if isinstance(clients, dict) else (clients,)
    seen: set[int] = set()
    for client in values:
        if id(client) in seen:
            continue
        seen.add(id(client))
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result
