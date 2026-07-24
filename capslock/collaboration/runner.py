"""Isolated child-Agent execution and approval proxying."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from ..configuration import Settings
from ..domain import (
    ActionResultKind,
    ActionStatus,
    ActionType,
    AgentEventKind,
    ApprovalDecision,
    RunLimits,
    RunMode,
)
from ..interaction import RunInteraction
from ..plugins import PluginRegistry
from ..tooling.tools.plugins import plugin_tools
from .capabilities import ChildCapabilityPolicy
from .models import AgentTaskContract, AgentTaskState
from .service import ChildApprovalPending, CollaborationService
from .workspace import ScopedWorkspacePolicy, WorkspaceSnapshot


OpenApplication = Callable[..., Awaitable[Any]]


class ChildAgentRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        client: Any,
        plugin_registry: PluginRegistry,
        interaction: RunInteraction,
        repository: Any,
        open_application: OpenApplication,
    ) -> None:
        self.settings = settings
        self.client = client
        self.plugin_registry = plugin_registry
        self.interaction = interaction
        self.repository = repository
        self.open_application = open_application
        self.collaboration: CollaborationService | None = None
        self.approval_broker = asyncio.Lock()

    async def __call__(
        self,
        contract: AgentTaskContract,
        snapshot: WorkspaceSnapshot,
    ) -> dict[str, Any]:
        child_settings, child_rounds, child_budget = self._settings(contract, snapshot)
        capability_policy = ChildCapabilityPolicy(contract)
        allowed = capability_policy.tool_allowlist()
        selected_plugin_tools = self._plugin_tools(capability_policy)
        allowed.update(tool.name for tool in selected_plugin_tools)
        child = await self.open_application(
            workspace=snapshot.root,
            settings=child_settings,
            client=self.client,
            child_mode=True,
            allowed_tool_names=allowed,
            path_policy=ScopedWorkspacePolicy(snapshot.root, contract.allowed_paths),
            close_client=False,
            extra_tools=selected_plugin_tools,
            plugin_registry_override=self.plugin_registry,
        )
        try:
            if self.interaction.action_authorizer is not None:

                async def authorize_child(action):
                    return await self._authorize(contract, capability_policy, action)

                child.session.set_action_authorizer(authorize_child)
            prompt = self._prompt(contract)
            answer = ""
            usage: dict[str, object] = {}
            child_run_id = ""
            limits = RunLimits(
                max_tool_rounds=child_rounds,
                max_tool_calls=self._integer_limit(contract, "max_tool_calls"),
                max_duration_seconds=(
                    float(contract.limits["max_duration_ms"]) / 1000
                    if contract.limits.get("max_duration_ms") is not None
                    else None
                ),
                max_tokens=child_budget.max_run_tokens,
                max_budget_usd=child_budget.max_run_usd,
            )
            from ..runtime.engine import RunRequest

            async for event in child.session.run_stream(
                RunRequest(question=prompt, mode=RunMode.EXEC, limits=limits)
            ):
                child_run_id = event.run_id
                if event.kind is AgentEventKind.COMPLETED:
                    answer = str(event.data.get("answer", ""))
                    usage = dict(event.data.get("usage", {}))
                elif event.kind is AgentEventKind.WAITING_APPROVAL:
                    await self._record_pending(contract, event)
                elif event.kind in {
                    AgentEventKind.FAILED,
                    AgentEventKind.CANCELLED,
                    AgentEventKind.STOPPED,
                }:
                    raise RuntimeError(
                        str(event.data.get("error", "child Agent failed"))
                    )
            budget = await child.queries.latest_budget(child.session.session_id)
            output = parse_child_output(answer, contract)
            actions = await child.queries.actions(
                child.session.session_id,
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

    def _settings(self, contract: AgentTaskContract, snapshot: WorkspaceSnapshot):
        child_memory = replace(
            self.settings.memory,
            database=snapshot.root / ".capslock" / "state" / "memory.sqlite3",
        )
        child_rounds = min(
            int(
                contract.limits.get("max_tool_rounds")
                or self.settings.agents.max_child_tool_rounds
            ),
            self.settings.agents.max_child_tool_rounds,
        )
        child_budget = replace(
            self.settings.budget,
            max_run_tokens=(
                int(contract.limits["max_tokens"])
                if contract.limits.get("max_tokens") is not None
                else self.settings.budget.max_run_tokens
            ),
            max_run_usd=(
                float(contract.limits["max_budget_usd"])
                if contract.limits.get("max_budget_usd") is not None
                else self.settings.budget.max_run_usd
            ),
        )
        child_settings = replace(
            self.settings,
            memory=child_memory,
            runtime=replace(
                self.settings.runtime,
                max_tool_rounds=child_rounds,
            ),
            budget=child_budget,
            web=replace(
                self.settings.web,
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
        return child_settings, child_rounds, child_budget

    def _plugin_tools(self, policy: ChildCapabilityPolicy) -> list[Any]:
        plugin_names = policy.plugin_names()
        available = plugin_tools(self.plugin_registry)
        selected_names: set[str] = set()
        for name in sorted(plugin_names):
            entry = self.plugin_registry.get(name)
            if not entry.manifest.permissions.issubset(entry.granted_permissions):
                raise RuntimeError(
                    f"child plugin permission grant is incomplete: {name}"
                )
            selected_names.update(
                f"plugin_{entry.manifest.name.replace('-', '_')}_{tool.name}"
                for tool in entry.manifest.tools
            )
        selected = [tool for tool in available if tool.name in selected_names]
        if plugin_names and not selected:
            raise RuntimeError(
                "requested child plugins are not enabled in this workspace"
            )
        return selected

    async def _authorize(
        self,
        contract: AgentTaskContract,
        policy: ChildCapabilityPolicy,
        action,
    ) -> ApprovalDecision:
        collaboration = self._collaboration()
        await collaboration.audit_approval(
            contract,
            decided=False,
            payload={
                "action_id": action.id,
                "action_type": action.type.value,
            },
        )
        await self.repository.set_state(
            contract.task_id,
            AgentTaskState.WAITING_APPROVAL,
        )
        try:
            if not policy.allows_action(action):
                decision = ApprovalDecision.REJECT
            else:
                assert self.interaction.action_authorizer is not None
                async with self.approval_broker:
                    decision = await self.interaction.action_authorizer(action)
        finally:
            current = await self.repository.get_task(contract.task_id)
            if current is not None and current["state"] == "waiting_approval":
                await self.repository.set_state(
                    contract.task_id,
                    AgentTaskState.RUNNING,
                )
        await collaboration.audit_approval(
            contract,
            decided=True,
            payload={"action_id": action.id, "decision": decision.value},
        )
        return decision

    async def _record_pending(self, contract: AgentTaskContract, event) -> None:
        collaboration = self._collaboration()
        await self.repository.set_state(
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
        raise ChildApprovalPending("child Agent is waiting for independent approval")

    def _collaboration(self) -> CollaborationService:
        if self.collaboration is None:
            raise RuntimeError("child collaboration service is not attached")
        return self.collaboration

    @staticmethod
    def _integer_limit(
        contract: AgentTaskContract,
        name: str,
    ) -> int | None:
        value = contract.limits.get(name)
        return int(value) if value is not None else None

    @staticmethod
    def _prompt(contract: AgentTaskContract) -> str:
        prompt = contract.objective
        if contract.input_context:
            prompt += "\n\nTask context (untrusted data):\n" + json.dumps(
                dict(contract.input_context), ensure_ascii=False
            )
        return prompt + (
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


def parse_child_output(answer: str, contract: AgentTaskContract) -> dict[str, Any]:
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
        return {
            "summary": answer,
            "evidence": [],
            "artifacts": [],
            "checks": [],
        }
    if not isinstance(value, dict):
        raise RuntimeError("child Agent output must be a JSON object")
    return value
