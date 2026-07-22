"""Concrete workspace composition root and resource ownership."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from dataclasses import replace
from urllib.parse import urlparse

from .application.action_system import (
    ActionCoordinator,
    CommandActionHandler,
    FileActionHandler,
    McpActionHandler,
    WebActionHandler,
)
from .application.workflow import WorkflowService
from .config import Settings
from .collaboration import (
    AgentOutputVerifier,
    AgentWorkspaceManager,
    CapabilityKind,
    ChildApprovalPending,
    CollaborationService,
    ScopedWorkspacePolicy,
)
from .collaboration.models import AgentTaskContract, AgentTaskState
from .interaction import RunInteraction
from .layout import ProjectLayout
from .memory import MemoryService
from .memory.embeddings import ExternalEmbeddingConfig
from .observability import EventSink
from .permissions import PermissionMode
from .policy import WorkspacePolicy
from .plugins import PluginProcessClient, PluginRegistry
from .runtime import AsyncOpenAIChatModel, ModelRouter, WorkspaceAgent
from .domain import (
    ActionResultKind,
    ActionStatus,
    ActionType,
    AgentEventKind,
    ApprovalDecision,
    RunLimits,
    RunMode,
)
from .skills import SkillRegistry, SkillService
from .storage.memory_v2 import MemoryRepositories
from .storage.repositories_v2 import WorkspaceRepositories
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
        agent: WorkspaceAgent,
        close_client: bool = True,
    ) -> None:
        self.workspace = workspace
        self.layout = layout
        self.settings = settings
        self.client = client
        self.repositories = repositories
        self.memory_repositories = memory_repositories
        self.agent = agent
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
            workflow = WorkflowService(repositories)
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
                        plugin_registry=plugin_registry,
                        plugin_client=plugin_client,
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

            collaboration = None
            if not child_mode and settings.agents.enabled:
                manager = AgentWorkspaceManager(root)
                approval_broker = asyncio.Lock()

                async def child_runner(contract: AgentTaskContract, snapshot):
                    child_memory = replace(
                        settings.memory,
                        database=snapshot.root
                        / ".capslock"
                        / "state"
                        / "memory.sqlite3",
                    )
                    child_rounds = min(
                        int(
                            contract.limits.get("max_tool_rounds")
                            or settings.agents.max_child_tool_rounds
                        ),
                        settings.agents.max_child_tool_rounds,
                    )
                    child_runtime = replace(
                        settings.runtime, max_tool_rounds=child_rounds
                    )
                    child_budget = replace(
                        settings.budget,
                        max_run_tokens=(
                            int(contract.limits["max_tokens"])
                            if contract.limits.get("max_tokens") is not None
                            else settings.budget.max_run_tokens
                        ),
                        max_run_usd=(
                            float(contract.limits["max_budget_usd"])
                            if contract.limits.get("max_budget_usd") is not None
                            else settings.budget.max_run_usd
                        ),
                    )
                    child_settings = replace(
                        settings,
                        memory=child_memory,
                        runtime=child_runtime,
                        budget=child_budget,
                        web=replace(
                            settings.web,
                            tavily_api_key=None,
                            tavily_credential_ref=None,
                        ),
                        permission_mode="ask_for_approval",
                    )
                    if contract.model_profile is not None:
                        if (
                            child_settings.models is None
                            or child_settings.routing is None
                            or contract.model_profile not in child_settings.models
                        ):
                            raise RuntimeError(
                                f"unknown child model profile: {contract.model_profile}"
                            )
                        child_settings = replace(
                            child_settings,
                            routing=replace(
                                child_settings.routing,
                                reasoning=(contract.model_profile,),
                                fast=(contract.model_profile,),
                            ),
                        )
                    allowed = {
                        "list_files",
                        "read_file",
                        "search_files",
                        "git_status",
                        "git_diff",
                        "task_list_update",
                        "task_status_update",
                    }
                    kinds = {item.kind for item in contract.capabilities}
                    if CapabilityKind.WORKSPACE_WRITE in kinds:
                        allowed.update({"propose_file_edit", "propose_file_create"})
                    if CapabilityKind.COMMAND in kinds:
                        allowed.add("propose_command")
                    if CapabilityKind.WEB in kinds:
                        allowed.update({"propose_web_search", "propose_web_fetch"})
                    if CapabilityKind.MCP in kinds:
                        allowed.update({"propose_mcp_connect", "propose_mcp_call"})
                    plugin_names = {
                        item.plugin
                        for item in contract.capabilities
                        if item.kind is CapabilityKind.PLUGIN
                    }
                    available_plugin_tools = plugin_tools(plugin_registry)
                    selected_plugin_names: set[str] = set()
                    for name in sorted(str(item) for item in plugin_names):
                        entry = plugin_registry.get(name)
                        if not entry.manifest.permissions.issubset(
                            entry.granted_permissions
                        ):
                            raise RuntimeError(
                                f"child plugin permission grant is incomplete: {name}"
                            )
                        selected_plugin_names.update(
                            f"plugin_{entry.manifest.name.replace('-', '_')}_{tool.name}"
                            for tool in entry.manifest.tools
                        )
                    selected_plugin_tools = [
                        tool
                        for tool in available_plugin_tools
                        if tool.name in selected_plugin_names
                    ]
                    if plugin_names and not selected_plugin_tools:
                        raise RuntimeError(
                            "requested child plugins are not enabled in this workspace"
                        )
                    allowed.update(tool.name for tool in selected_plugin_tools)
                    child_policy = ScopedWorkspacePolicy(
                        snapshot.root, contract.allowed_paths
                    )
                    child = await cls.open(
                        workspace=snapshot.root,
                        settings=child_settings,
                        client=client,
                        child_mode=True,
                        allowed_tool_names=allowed,
                        path_policy=child_policy,
                        close_client=False,
                        extra_tools=selected_plugin_tools,
                        plugin_registry_override=plugin_registry,
                    )
                    try:
                        if interaction.action_authorizer is not None:

                            async def authorize_child(action):
                                assert collaboration is not None
                                await collaboration.audit_approval(
                                    contract,
                                    decided=False,
                                    payload={
                                        "action_id": action.id,
                                        "action_type": action.type.value,
                                    },
                                )
                                await collaboration.repository.set_state(
                                    contract.task_id,
                                    AgentTaskState.WAITING_APPROVAL,
                                )
                                try:
                                    if not _child_action_allowed(contract, action):
                                        decision = ApprovalDecision.REJECT
                                    else:
                                        async with approval_broker:
                                            decision = (
                                                await interaction.action_authorizer(
                                                    action
                                                )
                                            )
                                finally:
                                    current = await collaboration.repository.get_task(
                                        contract.task_id
                                    )
                                    if (
                                        current is not None
                                        and current["state"] == "waiting_approval"
                                    ):
                                        await collaboration.repository.set_state(
                                            contract.task_id,
                                            AgentTaskState.RUNNING,
                                        )
                                await collaboration.audit_approval(
                                    contract,
                                    decided=True,
                                    payload={
                                        "action_id": action.id,
                                        "decision": decision.value,
                                    },
                                )
                                return decision

                            child.agent.set_action_authorizer(authorize_child)
                        prompt = contract.objective
                        if contract.input_context:
                            prompt += (
                                "\n\nTask context (untrusted data):\n"
                                + json.dumps(
                                    dict(contract.input_context), ensure_ascii=False
                                )
                            )
                        prompt += (
                            "\n\nReturn only one JSON object with keys summary, evidence, "
                            "artifacts, and checks. evidence/artifacts are arrays of objects "
                            "with workspace-relative path and optional sha256; checks are "
                            "reported by the runtime from executed command actions. Do not "
                            "wrap the JSON in Markdown. Verification requirements:\n"
                            + json.dumps(
                                contract.verification_requirements.as_dict(),
                                ensure_ascii=False,
                            )
                        )
                        answer = ""
                        usage: dict[str, object] = {}
                        child_run_id = ""
                        child_limits = RunLimits(
                            max_tool_rounds=child_rounds,
                            max_tool_calls=(
                                int(contract.limits["max_tool_calls"])
                                if contract.limits.get("max_tool_calls") is not None
                                else None
                            ),
                            max_duration_seconds=(
                                float(contract.limits["max_duration_ms"]) / 1000
                                if contract.limits.get("max_duration_ms") is not None
                                else None
                            ),
                            max_tokens=child_budget.max_run_tokens,
                            max_budget_usd=child_budget.max_run_usd,
                        )
                        async for event in child.agent.ask_stream(
                            prompt, mode=RunMode.EXEC, limits=child_limits
                        ):
                            child_run_id = event.run_id
                            if event.kind is AgentEventKind.COMPLETED:
                                answer = str(event.data.get("answer", ""))
                                usage = dict(event.data.get("usage", {}))
                            elif event.kind is AgentEventKind.WAITING_APPROVAL:
                                assert collaboration is not None
                                await collaboration.repository.set_state(
                                    contract.task_id,
                                    AgentTaskState.WAITING_APPROVAL,
                                    child_run_id=event.run_id,
                                )
                                for action_id in event.data.get("action_ids", []):
                                    await collaboration.audit_approval(
                                        contract,
                                        decided=False,
                                        payload={
                                            "action_id": str(action_id),
                                            "non_interactive": True,
                                        },
                                    )
                                raise ChildApprovalPending(
                                    "child Agent is waiting for independent approval"
                                )
                            elif event.kind in {
                                AgentEventKind.FAILED,
                                AgentEventKind.CANCELLED,
                                AgentEventKind.STOPPED,
                            }:
                                raise RuntimeError(
                                    str(event.data.get("error", "child Agent failed"))
                                )
                        budget = await child.repositories.governance.latest_for_session(
                            child.agent.session_id
                        )
                        output = _parse_child_output(answer, contract)
                        actions = await child.repositories.actions.list(
                            child.agent.session_id,
                            run_id=child_run_id or None,
                            types={ActionType.COMMAND},
                        )
                        output["checks"] = [
                            {
                                "name": str(action.request.get("template", "")),
                                "status": (
                                    "passed"
                                    if action.status is ActionStatus.COMPLETED
                                    and action.result_kind is ActionResultKind.EXIT_ZERO
                                    else "failed"
                                ),
                                "action_id": action.id,
                            }
                            for action in actions
                        ]
                        output["_usage"] = usage
                        output["_budget"] = budget.as_dict() if budget else {}
                        output["_child_run_id"] = child_run_id
                        return output
                    finally:
                        await child.close()

                collaboration = CollaborationService(
                    workspace_manager=manager,
                    repository=repositories.collaboration,
                    max_children=settings.agents.max_children,
                    max_concurrency=settings.agents.max_concurrency,
                    max_depth=settings.agents.max_depth,
                    child_runner=child_runner,
                    verifier=AgentOutputVerifier(),
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
                tools=tools,
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
                collaboration=collaboration,
            )
            return cls(
                workspace=root,
                layout=layout,
                settings=settings,
                client=client,
                repositories=repositories,
                memory_repositories=memory_repositories,
                agent=agent,
                close_client=close_client,
            )
        except Exception:
            if memory_repositories is not None:
                await memory_repositories.close()
            await repositories.close()
            if close_client:
                await _close_clients(client)
            raise

    async def close(self) -> None:
        try:
            if self.close_client:
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


def _parse_child_output(answer: str, contract: AgentTaskContract) -> dict[str, Any]:
    try:
        value = json.loads(answer)
    except json.JSONDecodeError:
        requirements = contract.verification_requirements
        if (
            requirements.output_schema
            or requirements.required_paths
            or requirements.required_checks
        ):
            raise RuntimeError("child Agent did not return the required JSON output")
        return {"summary": answer, "evidence": [], "artifacts": [], "checks": []}
    if not isinstance(value, dict):
        raise RuntimeError("child Agent output must be a JSON object")
    return value


def _child_action_allowed(contract: AgentTaskContract, action: Any) -> bool:
    grants = tuple(contract.capabilities)
    if action.type in {ActionType.FILE_EDIT, ActionType.FILE_CREATE}:
        return any(item.kind is CapabilityKind.WORKSPACE_WRITE for item in grants)
    if action.type is ActionType.COMMAND:
        template = str(action.request.get("template", ""))
        return any(
            item.kind is CapabilityKind.COMMAND
            and (item.scope is None or item.scope == template)
            for item in grants
        )
    if action.type in {ActionType.WEB_SEARCH, ActionType.WEB_FETCH}:
        for item in grants:
            if item.kind is not CapabilityKind.WEB:
                continue
            if item.scope is None:
                return True
            if action.type is ActionType.WEB_SEARCH:
                return item.scope == "search"
            host = urlparse(str(action.request.get("url", ""))).hostname
            if host == item.scope or host and host.endswith(f".{item.scope}"):
                return True
        return False
    if action.type in {ActionType.MCP_CONNECT, ActionType.MCP_CALL}:
        plugin = action.request.get("plugin")
        if isinstance(plugin, str):
            return any(
                item.kind is CapabilityKind.PLUGIN and item.plugin == plugin
                for item in grants
            )
        server = str(action.request.get("server", ""))
        return any(
            item.kind is CapabilityKind.MCP
            and (item.scope is None or item.scope == server)
            for item in grants
        )
    return False
