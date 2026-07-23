"""Concrete workspace composition root and resource ownership."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from .application.action_system import (
    ActionCoordinator,
    ActionRunState,
    CommandActionHandler,
    CredentialActionHandler,
    FileActionHandler,
    McpActionHandler,
    WebActionHandler,
    resolve_named_credential,
)
from .application.workflow import WorkflowService
from .application.queries import WorkspaceQueries
from .configuration import Settings
from .domain import ActionRecord, ActionStatus, ActionType
from .collaboration import (
    AgentOutputVerifier,
    ChildAgentRunner,
    AgentWorkspaceManager,
    CollaborationService,
)
from .interaction import RunInteraction
from .layout import ProjectLayout
from .memory import MemoryService
from .memory.embeddings import ExternalEmbeddingConfig
from .observability import EventSink
from .permissions import PermissionMode
from .policy import WorkspacePolicy
from .plugins import PluginProcessClient, PluginRegistry
from .plugins.broker import BrokerCallbacks
from .policy import PolicyError
from .runtime import AgentSession, AsyncOpenAIChatModel, ModelRouter
from .skills import SkillRegistry, SkillService
from .storage.memory_repositories import MemoryRepositories
from .storage.repositories import WorkspaceRepositories
from .storage.artifacts import ToolArtifactStore
from .tooling.async_catalog import workspace_tools
from .tooling.plugins import plugin_tools


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
        session: AgentSession,
        close_client: bool = True,
    ) -> None:
        self.workspace = workspace
        self.layout = layout
        self.settings = settings
        self.client = client
        self._repositories = repositories
        self._memory_repositories = memory_repositories
        self.session = session
        self.queries = WorkspaceQueries(
            repositories.sessions,
            repositories.runs,
            repositories.work_items,
            repositories.run_journal,
            repositories.actions,
            repositories.tasks,
            repositories.sources,
            repositories.governance,
            repositories.collaboration,
        )
        self.close_client = close_client

    @classmethod
    async def open(
        cls,
        *,
        workspace: Path,
        settings: Settings,
        client: Any,
        session_id: str | None = None,
        layout: ProjectLayout | None = None,
        child_mode: bool = False,
        allowed_tool_names: set[str] | None = None,
        path_policy: WorkspacePolicy | None = None,
        close_client: bool = True,
        extra_tools: list[Any] | None = None,
        plugin_registry_override: PluginRegistry | None = None,
    ) -> "WorkspaceApplication":
        root = workspace.resolve()
        layout = layout or ProjectLayout.discover(root)
        resources = AsyncExitStack()
        await resources.__aenter__()
        if close_client:
            resources.push_async_callback(_close_clients, client)
        try:
            memory_path = settings.memory.database or layout.user.canonical_memory
            repositories, memory_repositories = await asyncio.gather(
                WorkspaceRepositories.open(layout.database, workspace=root),
                MemoryRepositories.open(memory_path),
            )
            resources.push_async_callback(repositories.close)
            resources.push_async_callback(memory_repositories.close)
            artifacts = ToolArtifactStore(layout.artifacts, repositories.database)
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
            plugin_registry = plugin_registry_override or PluginRegistry(layout)
            plugin_client = PluginProcessClient(
                timeout_seconds=settings.mcp.mcp_timeout_seconds,
                output_limit_bytes=settings.mcp.mcp_output_bytes,
            )
            tools = workspace_tools(include_collaboration=not child_mode).combined(
                extra_tools
                if child_mode and extra_tools is not None
                else []
                if child_mode
                else plugin_tools(plugin_registry)
            )
            if allowed_tool_names is not None:
                tools = tools.filtered(allowed_tool_names)
            workflow = WorkflowService(
                repositories.work_items,
                repositories.runs,
                repositories.run_journal,
                repositories.workflow,
            )
            policy = path_policy or WorkspacePolicy(root)
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
            primary_profile = (settings.models or {})[
                settings.routing.reasoning[0]
            ]
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
                source_validator=repositories.runs.completed,
                external_embedding_profiles=external_embedding_profiles,
            )

            def actions(run_id: str) -> ActionCoordinator:
                coordinator: ActionCoordinator | None = None

                def broker_callbacks(parent: ActionRecord) -> BrokerCallbacks:
                    async def execute_capability(
                        action_type: ActionType, payload: dict[str, object]
                    ) -> dict[str, object]:
                        if coordinator is None:
                            raise PolicyError("plugin capability approval is unavailable")
                        action = await coordinator.propose(action_type, **payload)
                        if action.status is not ActionStatus.COMPLETED:
                            raise PolicyError(
                                "plugin capability request was not approved and completed"
                            )
                        return dict(action.result or {})

                    async def workspace_write(
                        params: dict[str, Any],
                    ) -> dict[str, object]:
                        path, content = params.get("path"), params.get("content")
                        if not isinstance(path, str) or not isinstance(content, str):
                            raise PolicyError(
                                "workspace write requires string path and content"
                            )
                        target = policy.writable_file(path, create=True)
                        payload: dict[str, object] = {
                            "path": path,
                            "summary": f"Plugin {parent.request.get('plugin')} writes {path}",
                        }
                        if target.exists():
                            payload["replace_content"] = content
                            action_type = ActionType.FILE_EDIT
                        else:
                            payload["content"] = content
                            action_type = ActionType.FILE_CREATE
                        return await execute_capability(action_type, payload)

                    async def network(params: dict[str, Any]) -> dict[str, object]:
                        return await execute_capability(
                            ActionType.WEB_FETCH, {"url": params.get("url")}
                        )

                    async def process(params: dict[str, Any]) -> dict[str, object]:
                        return await execute_capability(
                            ActionType.COMMAND,
                            {
                                "template": params.get("template"),
                                "target": params.get("target"),
                                "cwd": params.get("cwd", "."),
                            },
                        )

                    async def credential(params: dict[str, Any]) -> dict[str, object]:
                        name = params.get("name")
                        if not isinstance(name, str):
                            raise PolicyError("credential capability requires a name")
                        await execute_capability(
                            ActionType.CREDENTIAL_ACCESS, {"name": name}
                        )
                        secret = await asyncio.to_thread(resolve_named_credential, name)
                        return {"name": name, "value": secret}

                    return BrokerCallbacks(
                        workspace_write=workspace_write,
                        network=network,
                        process=process,
                        credential=credential,
                    )

                handlers = [
                    FileActionHandler(policy),
                    CommandActionHandler(
                        policy,
                        timeout_seconds=settings.command.command_timeout_seconds,
                        output_limit_bytes=settings.command.command_output_bytes,
                    ),
                    WebActionHandler(
                        repositories.sources,
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
                        plugin_registry=plugin_registry,
                        plugin_client=plugin_client,
                        broker_callbacks=broker_callbacks,
                    ),
                    CredentialActionHandler(),
                ]
                coordinator = ActionCoordinator(
                    repositories.actions,
                    ActionRunState(repositories.runs, repositories.workflow),
                    session_id=session.id,
                    run_id=run_id,
                    handlers=handlers,
                    event=events.emit,
                    interaction=interaction,
                )
                return coordinator

            collaboration = None
            if not child_mode and settings.agents.enabled:
                manager = AgentWorkspaceManager(root)
                child_runner = ChildAgentRunner(
                    settings=settings,
                    client=client,
                    plugin_registry=plugin_registry,
                    interaction=interaction,
                    repository=repositories.collaboration,
                    open_application=cls.open,
                )
                collaboration = CollaborationService(
                    workspace_manager=manager,
                    repository=repositories.collaboration,
                    max_children=settings.agents.max_children,
                    max_concurrency=settings.agents.max_concurrency,
                    max_depth=settings.agents.max_depth,
                    child_runner=child_runner,
                    verifier=AgentOutputVerifier(),
                )
                child_runner.collaboration = collaboration

            agent_session = AgentSession(
                workspace=root,
                model_name=session.model,
                chat_model=router,
                sessions=repositories.sessions,
                work_items=repositories.work_items,
                runs=repositories.runs,
                journal=repositories.run_journal,
                action_records=repositories.actions,
                tasks=repositories.tasks,
                sources=repositories.sources,
                settings_store=repositories.settings,
                model_audit=repositories.models,
                governance=repositories.governance,
                collaboration_records=repositories.collaboration,
                compactions=repositories.compactions,
                workflow=workflow,
                session_id=session.id,
                policy=policy,
                action_factory=actions,
                skill_registry=skill_registry,
                skill_service=skill_service,
                tools=tools,
                events=events,
                memory=memory,
                permission_mode=permission_mode,
                max_tool_rounds=settings.runtime.max_tool_rounds,
                context_settings=settings.context,
                context_window=primary_profile.context_window,
                max_output_tokens=primary_profile.max_output_tokens,
                model_profile=primary_profile.name,
                artifacts=artifacts,
                input_cost_per_million=settings.model_config.input_cost_per_million,
                output_cost_per_million=settings.model_config.output_cost_per_million,
                max_run_tokens=settings.budget.max_run_tokens,
                max_run_usd=settings.budget.max_run_usd,
                loop_detection=settings.loop_detection,
                interaction=interaction,
                collaboration=collaboration,
            )
            application = cls(
                workspace=root,
                layout=layout,
                settings=settings,
                client=client,
                repositories=repositories,
                memory_repositories=memory_repositories,
                session=agent_session,
                close_client=close_client,
            )
            resources.pop_all()
            return application
        except Exception:
            await resources.aclose()
            raise

    async def close(self) -> None:
        self.session.events.flush()
        try:
            if self.close_client:
                await _close_clients(self.client)
        finally:
            try:
                await self._repositories.close()
            finally:
                await self._memory_repositories.close()

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
