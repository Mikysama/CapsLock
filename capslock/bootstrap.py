"""Concrete workspace composition root and resource ownership."""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from .application.action_system import (
    WorkspaceExecutionScope,
)
from .application.workflow import WorkflowService
from .application.queries import WorkspaceQueries
from .configuration import Settings
from .domain import ModelRole
from .composition import (
    build_action_factory,
    build_collaboration,
    build_integrations,
    build_tool_runtime,
)
from .interaction import RunInteraction
from .layout import ProjectLayout
from .memory import MemoryService
from .memory.recall import RecallPolicy
from .memory.embeddings import ExternalEmbeddingConfig
from .observability import EventSink
from .permissions import PermissionMode
from .planning import PlanningService
from .policy import WorkspacePolicy
from .plugins import PluginProcessClient, PluginRegistry
from .lsp import LspManager
from .mcp import McpManager
from .runtime import AgentSession, AsyncOpenAIResponsesModel, ModelRouter
from .runtime.agent import INSTRUCTIONS
from .runtime.context import ContextEvaluationPolicy
from .shell import ModelShellClassifier, SessionProcessManager
from .skills import SkillRegistry, SkillService
from .storage.memory_repositories import MemoryRepositories
from .storage.retention import run_retention_maintenance
from .storage.repositories import WorkspaceRepositories
from .workspace_writes import WorkspaceMutationCoordinator
from .storage.artifacts import ToolArtifactStore
from .bridge import IdeBridgeServer


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
        process_manager: SessionProcessManager | None = None,
        mcp_manager: McpManager | None = None,
        lsp_manager: LspManager | None = None,
        plugin_client: PluginProcessClient | None = None,
        ide_bridge: IdeBridgeServer | None = None,
        close_client: bool = True,
    ) -> None:
        self.workspace = workspace
        self.layout = layout
        self.settings = settings
        self.client = client
        self._repositories = repositories
        self._memory_repositories = memory_repositories
        self.session = session
        self._process_manager = process_manager
        self._mcp_manager = mcp_manager
        self._lsp_manager = lsp_manager
        self._plugin_client = plugin_client
        self._ide_bridge = ide_bridge
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

    def mcp_statuses(self):
        """Expose safe MCP runtime status to every presentation layer."""
        return self._mcp_manager.statuses() if self._mcp_manager is not None else ()

    @property
    def repositories(self) -> WorkspaceRepositories:
        """Command-service persistence surface owned by this application."""
        return self._repositories

    @property
    def memory_repositories(self) -> MemoryRepositories:
        return self._memory_repositories

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
        core_instructions: str | None = None,
        runtime_controls: tuple[str, ...] = (),
        memory_recall_policy: RecallPolicy | None = None,
        context_evaluation_policy: ContextEvaluationPolicy | None = None,
    ) -> "WorkspaceApplication":
        root = workspace.resolve()
        layout = layout or ProjectLayout.discover(root)
        resources = AsyncExitStack()
        await resources.__aenter__()
        if close_client:
            resources.push_async_callback(_close_clients, client)
        try:
            memory_path = settings.memory.database or layout.user.canonical_memory
            repositories = await WorkspaceRepositories.open(
                layout.database, workspace=root, shared_owner=child_mode
            )
            resources.push_async_callback(repositories.close)
            memory_repositories = await MemoryRepositories.open(memory_path)
            resources.push_async_callback(memory_repositories.close)
            await run_retention_maintenance(
                repositories.database,
                memory_repositories.database,
                settings.storage,
            )
            if settings.observability.enabled:
                await repositories.performance.prune(
                    days=settings.observability.retention_days,
                    maximum=settings.observability.max_spans,
                )
            artifacts = ToolArtifactStore(
                layout.artifacts, repositories.database, repositories.episodic
            )
            if session_id is None:
                initial_profile = (settings.models or {})[settings.routing.reasoning[0]]
                session = await repositories.sessions.create(
                    initial_profile.model, model_profile=initial_profile.name
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
            planning = PlanningService(repositories.plans, root=layout.plans)
            await planning.reconcile(session.id)
            interaction = RunInteraction(permission_mode=permission_mode)
            events = EventSink(layout.events)
            disabled = await repositories.settings.disabled_skills()
            skill_registry = SkillRegistry(
                root,
                disabled=lambda name: name in disabled,
                layout=layout,
            )
            skill_service = SkillService(skill_registry, events.emit)
            policy = path_policy or WorkspacePolicy(root)
            ide_bridge = None
            if not child_mode and (
                settings.bridge.enabled or os.environ.get("CAPSLOCK_IDE") == "1"
            ):
                ide_bridge = IdeBridgeServer(
                    root,
                    layout.root / "state",
                    max_selection_bytes=settings.bridge.max_selection_bytes,
                    max_diagnostics=settings.bridge.max_diagnostics,
                )
                await ide_bridge.start()
                resources.push_async_callback(ide_bridge.close)
            execution_scope = WorkspaceExecutionScope(root, policy)
            write_coordinator = WorkspaceMutationCoordinator(
                layout.root / "state" / "workspace-write-locks"
            )
            active_worktree = await repositories.database.fetch_one(
                "SELECT path FROM session_worktrees WHERE session_id=? AND active=1",
                (session.id,),
            )
            if active_worktree is not None:
                active_path = Path(str(active_worktree["path"]))
                if active_path.is_dir() and (active_path / ".git").exists():
                    await execution_scope.switch(active_path)
                else:
                    await repositories.database.execute(
                        "UPDATE session_worktrees SET active=0,status='invalid' WHERE session_id=? AND active=1",
                        (session.id,),
                    )
            integrations = await build_integrations(
                policy=execution_scope.policy,
                settings=settings,
                layout=layout,
                journal=repositories.run_journal,
                resources=resources,
                child_mode=child_mode,
                plugin_registry=plugin_registry_override,
            )
            plugin_registry = integrations.plugins
            plugin_client = integrations.plugin_client
            permission_engine = integrations.permissions
            mcp_manager = integrations.mcp
            lsp_manager = integrations.lsp
            process_manager = integrations.processes
            tools = await build_tool_runtime(
                settings=settings,
                child_mode=child_mode,
                permission_engine=permission_engine,
                lsp=lsp_manager,
                mcp=mcp_manager,
                plugins=plugin_registry,
                extra_tools=extra_tools or (),
                allowed_names=allowed_tool_names,
                discoveries=await repositories.run_journal.tool_discoveries(session.id),
                planning=planning,
            )
            workflow = WorkflowService(
                repositories.work_items,
                repositories.runs,
                repositories.run_journal,
                repositories.workflow,
            )
            raw_clients = client if isinstance(client, dict) else {"default": client}
            adapters = {
                name: AsyncOpenAIResponsesModel(
                    item,
                    strict_tools=True,
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
            from .runtime.profiles import resolve_profile

            primary_profile = (settings.models or {})[settings.routing.reasoning[0]]
            try:
                selected_profile = resolve_profile(
                    settings.models or {},
                    profile_id=session.model_profile,
                    model=None if session.model_profile else session.model,
                )
            except ValueError:
                selected_profile = None
            if selected_profile is not None:
                primary_profile = selected_profile
                if (
                    session.model_profile is None
                    or session.model != selected_profile.model
                ):
                    session = await repositories.sessions.set_model_profile(
                        session.id, selected_profile.name, selected_profile.model
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
                manual_write_enabled=settings.memory.manual_write_enabled,
                capture_enabled=settings.memory.capture_enabled,
                recall_enabled=settings.memory.recall_enabled,
                maintenance_enabled=settings.memory.maintenance_enabled,
                capture_policy=settings.memory.policy,
                temporary_ttl_days=settings.memory.temporary_ttl_days,
                project_instance_id=repositories.project_instance_id,
                conversation_source=repositories.sessions,
                model_profile=primary_profile.name,
                event=events.emit,
                source_validator=repositories.runs.completed,
                task_repository=repositories.tasks,
                external_embedding_profiles=external_embedding_profiles,
                recall_policy=memory_recall_policy,
            )
            await memory.reconcile_lifecycle()
            await memory.recover_jobs()

            actions = build_action_factory(
                settings=settings,
                repositories=repositories,
                session_id=session.id,
                layout=layout,
                scope=execution_scope,
                lsp=lsp_manager,
                mcp=mcp_manager,
                plugins=plugin_registry,
                plugin_client=plugin_client,
                processes=process_manager,
                interaction=interaction,
                permission_engine=permission_engine,
                emit=events.emit,
                write_coordinator=write_coordinator,
            )
            collaboration = build_collaboration(
                settings=settings,
                child_mode=child_mode,
                active_root=execution_scope.active_root,
                state_root=layout.root / "state" / "agents",
                client=client,
                plugins=plugin_registry,
                mcp=mcp_manager,
                interaction=interaction,
                repository=repositories.collaboration,
                action_repository=repositories.actions,
                open_application=cls.open,
                memory=memory,
                write_coordinator=write_coordinator,
            )

            agent_session = AgentSession(
                workspace=execution_scope.active_root,
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
                policy=execution_scope.policy,
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
                permission_engine=permission_engine,
                process_manager=process_manager,
                max_read_concurrency=settings.tools.max_read_concurrency,
                aggregate_result_bytes=settings.tools.aggregate_result_bytes,
                max_argument_repair_attempts=(
                    settings.tools.max_argument_repair_attempts
                ),
                core_instructions=core_instructions or INSTRUCTIONS,
                runtime_controls=runtime_controls,
                shell_classifier_factory=(
                    lambda model_session: (
                        ModelShellClassifier(
                            model_session.for_role(ModelRole.FAST),
                            model_name=session.model,
                            threshold=settings.shell.classifier_threshold,
                        )
                        if settings.shell.classifier_enabled
                        else None
                    )
                ),
                document_settings=settings.documents,
                planning=planning,
                performance=(
                    repositories.performance if settings.observability.enabled else None
                ),
                ide_bridge=ide_bridge,
                input_cost_per_million=primary_profile.input_cost_per_million,
                output_cost_per_million=primary_profile.output_cost_per_million,
                max_run_tokens=settings.budget.max_run_tokens,
                max_run_usd=settings.budget.max_run_usd,
                loop_detection=settings.loop_detection,
                interaction=interaction,
                collaboration=collaboration,
                context_evaluation_policy=context_evaluation_policy,
            )

            agent_session.model_profile_id = session.model_profile
            if selected_profile is not None:
                agent_session._apply_model_profile(selected_profile)

            async def switch_active_workspace(
                active_root: Path, active_policy: WorkspacePolicy
            ) -> None:
                agent_session.workspace = active_root
                agent_session.policy = active_policy
                if collaboration is not None:
                    collaboration.workspace_manager.parent_workspace = active_root
                switched = await asyncio.gather(
                    mcp_manager.switch_policy(active_policy),
                    lsp_manager.switch_policy(active_policy),
                    return_exceptions=True,
                )
                for subsystem, result in zip(("mcp", "lsp"), switched, strict=True):
                    if isinstance(result, BaseException):
                        events.emit(
                            "workspace_runtime_refresh_failed",
                            subsystem=subsystem,
                            error=str(result) or type(result).__name__,
                        )
                try:
                    await tools.refresh_dynamic()
                except Exception as exc:
                    events.emit(
                        "workspace_runtime_refresh_failed",
                        subsystem="tool_catalog",
                        error=str(exc) or type(exc).__name__,
                    )

            execution_scope.bind(switch_active_workspace)
            application = cls(
                workspace=root,
                layout=layout,
                settings=settings,
                client=client,
                repositories=repositories,
                memory_repositories=memory_repositories,
                session=agent_session,
                process_manager=process_manager,
                mcp_manager=mcp_manager,
                lsp_manager=lsp_manager,
                plugin_client=plugin_client,
                ide_bridge=ide_bridge,
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
            if self._process_manager is not None:
                await self._process_manager.close()
            if self._mcp_manager is not None:
                await self._mcp_manager.close()
            if self._lsp_manager is not None:
                await self._lsp_manager.close()
            if self._plugin_client is not None:
                await self._plugin_client.close()
            if self._ide_bridge is not None:
                await self._ide_bridge.close()
            if self.close_client:
                await _close_clients(self.client)
        finally:
            try:
                if self.session.memory is not None:
                    await self.session.memory.close()
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
