"""Focused services behind the public AgentSession façade."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

from ..domain import (
    ApprovalChoice,
    ApprovalDecision,
    AgentEvent,
    RunKind,
    RunMode,
    WorkItemStatus,
)
from ..permissions import PermissionMode
from ..security import redact
from ..tooling.contracts import ToolOutcome, ToolOutcomeStatus, ToolPause
from .engine import RunRequest


class SessionAdministration:
    def __init__(self, *, session_id, sessions, runs, work_items, workflow) -> None:
        self.session_id = session_id
        self.sessions = sessions
        self.runs = runs
        self.work_items = work_items
        self.workflow = workflow

    async def rename(self, title: str):
        return await self.sessions.rename(self.session_id, title)

    async def retryable_run(self, prefix: str):
        return await self.runs.retryable(self.session_id, prefix)

    async def queued_work_item(self, prefix: str):
        item = await self.work_items.require(prefix)
        if item.session_id != self.session_id:
            raise ValueError("work item does not belong to this session")
        return item

    async def cancel_queued_work_item(self, prefix: str):
        item = await self.queued_work_item(prefix)
        return await self.work_items.update(
            item.id,
            WorkItemStatus.CANCELLED,
            error="cancelled before start",
        )

    async def reorder_queued_work_item(self, prefix: str, position: int):
        item = await self.queued_work_item(prefix)
        return await self.work_items.reorder(item.id, position)

    async def delete_if_empty(self) -> bool:
        return await self.sessions.delete_if_empty(self.session_id)

    async def enqueue(
        self,
        question: str,
        *,
        parent_work_item_id: str | None = None,
        kind: RunKind = RunKind.AGENT,
    ):
        return await self.workflow.enqueue(
            self.session_id,
            question,
            parent_work_item_id=parent_work_item_id,
            kind=kind,
        )


class PlanRequestService:
    def __init__(
        self,
        *,
        session_id,
        planning,
        work_items,
        permission_mode: Callable[[], PermissionMode],
    ) -> None:
        self.session_id = session_id
        self.planning = planning
        self.work_items = work_items
        self._permission_mode = permission_mode

    async def current_plan(self):
        if self.planning is None:
            return None
        return await self.planning.current(self.session_id)

    async def plan_requests(self):
        if self.planning is None:
            return []
        return await self.planning.repository.pending_requests(self.session_id)

    async def resolve_plan_request(self, prefix: str):
        matches = [
            item for item in await self.plan_requests() if item.id.startswith(prefix)
        ]
        if not matches:
            raise ValueError("pending plan request does not exist")
        if len(matches) > 1:
            raise ValueError("plan request prefix is ambiguous")
        return matches[0]

    async def decide_plan_request(
        self, identifier: str, choice: str, *, feedback: str | None = None
    ):
        if self.planning is None:
            raise ValueError("planning service is unavailable")
        request = await self.planning.repository.decide(
            identifier,
            choice=choice,
            feedback=feedback,
            base_permission_mode=self._permission_mode().value,
        )
        if request.plan_id is not None:
            plan = await self.planning.repository.require(request.plan_id)
            revision = await self.planning.repository.current_revision(plan)
            if plan.status.value in {"draft", "awaiting_approval"}:
                await self.planning.sync_mirror(plan, revision)
        return request

    async def implementation_for_planning_run(self, run_id: str):
        if self.planning is None:
            return None
        request = await self.planning.repository.approved_request_for_run(run_id)
        if request is None or request.plan_id is None:
            return None
        implementation = await self.planning.repository.implementation(request.plan_id)
        return await self.work_items.require(implementation.work_item_id)


class PermissionRequestService:
    def __init__(
        self, *, session_id, journal, permission_engine, tools, context_factory
    ) -> None:
        self.session_id = session_id
        self.journal = journal
        self.permission_engine = permission_engine
        self.tools = tools
        self._context_factory = context_factory

    async def permission_requests(
        self, *, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        """Return durable non-Action permission requests for this session."""

        if not hasattr(self.journal, "list_permission_requests"):
            return []
        requests = await self.journal.list_permission_requests(
            self.session_id, status=status
        )
        for request in requests:
            invocation = await self.journal.tool_invocation(request["invocation_id"])
            request["preview"] = _permission_preview(
                invocation.get("arguments", {}) if invocation else {}
            )
        return requests

    async def permission_rules(self) -> list[dict[str, Any]]:
        if self.permission_engine is None:
            return []
        return [
            item.as_dict()
            for item in await self.permission_engine.rules(self.session_id)
        ]

    async def permission_diagnostics(self) -> tuple[str, ...]:
        if self.permission_engine is None:
            return ("permission engine is unavailable",)
        await self.permission_engine.rules(self.session_id)
        return self.permission_engine.diagnostics()

    async def recent_permission_decisions(
        self, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if not hasattr(self.journal, "recent_permission_decisions"):
            return []
        return await self.journal.recent_permission_decisions(
            self.session_id, limit=limit
        )

    async def trust_project_permissions(self) -> str:
        if self.permission_engine is None:
            raise ValueError("permission engine is unavailable")
        return await self.permission_engine.trust_project_permissions()

    async def apply_permission_update(self, raw: dict[str, Any]) -> str:
        if self.permission_engine is None:
            raise ValueError("permission engine is unavailable")
        update = _permission_update_for_management(raw)
        return await self.permission_engine.apply_update(self.session_id, update)

    async def resolve_permission_request(self, prefix: str) -> dict[str, Any]:
        matches = [
            item
            for item in await self.permission_requests(status="pending")
            if str(item["id"]).startswith(prefix)
        ]
        if not matches:
            raise ValueError("pending permission request does not exist")
        if len(matches) > 1:
            raise ValueError("permission request prefix is ambiguous")
        return matches[0]

    async def decide_permission_request(
        self,
        identifier: str,
        choice: ApprovalChoice | ApprovalDecision | str,
    ) -> dict[str, Any]:
        """Decide and, when approved, execute one paused non-Action invocation."""

        selected = _permission_approval_choice(choice)
        request = await self.journal.permission_request(
            identifier, session_id=self.session_id
        )
        if request is None or request["status"] != "pending":
            raise ValueError("permission request is not pending")
        invocation = await self.journal.tool_invocation(request["invocation_id"])
        if (
            invocation is None
            or invocation["session_id"] != self.session_id
            or invocation["run_id"] != request["run_id"]
            or invocation["name"] != request["tool"]
            or invocation["status"] != "waiting_approval"
        ):
            raise ValueError("paused permission invocation is unavailable or changed")

        tool = self.tools.get(str(request["tool"]))
        if tool is None:
            raise ValueError("the requested tool is no longer available")
        context = self._context_factory(str(request["run_id"]))
        context = replace(
            context,
            invocation_id=str(request["invocation_id"]),
            catalog=self.tools,
        )
        arguments = dict(invocation["arguments"])
        normalized = self.permission_engine.normalize(tool, arguments, context)
        digest = _permission_arguments_digest(normalized)
        if digest != request["arguments_sha256"]:
            raise ValueError("tool input changed after approval was requested")

        selected_update: dict[str, Any] | None = None
        if selected in {
            ApprovalChoice.APPROVE_SESSION,
            ApprovalChoice.APPROVE_LOCAL,
        }:
            destination = (
                "session" if selected is ApprovalChoice.APPROVE_SESSION else "local"
            )
            selected_update = next(
                (
                    item
                    for item in request.get("suggestions", [])
                    if isinstance(item, dict) and item.get("destination") == destination
                ),
                None,
            )
            if selected_update is None:
                raise ValueError(f"no {destination} permission suggestion is available")
            update = _permission_update(selected_update, expected_tool=tool.name)
            await self.permission_engine.apply_update(self.session_id, update)
            if not await self.permission_engine.verify_explicit_allow(
                session_id=self.session_id,
                tool=tool.name,
                arguments=normalized,
            ):
                raise ValueError(
                    "persisted permission is shadowed and cannot authorize this invocation"
                )

        decided = await self.journal.decide_permission_request(
            identifier,
            session_id=self.session_id,
            choice=selected.value,
            selected_update=selected_update,
        )
        if selected is ApprovalChoice.REJECT:
            outcome = ToolOutcome(
                ToolOutcomeStatus.DENIED,
                False,
                error="permission request was rejected",
                error_code="permission_rejected",
            )
        else:
            execution = await self.tools.invoke(tool.name, context, arguments)
            if isinstance(execution.execution, ToolPause):
                outcome = ToolOutcome.failure(
                    "approved invocation requested another pause and was not executed",
                    code="permission_resume_paused",
                )
            else:
                outcome = execution.execution
        result = json.loads(outcome.for_model())
        await self.journal.complete_permission_request(
            identifier, session_id=self.session_id, result=result
        )
        decided["result"] = result
        return decided


class RunExecutionCoordinator:
    def __init__(self, *, session_id, engine, runs) -> None:
        self.session_id = session_id
        self.engine = engine
        self.runs = runs

    async def run_stream(self, request: RunRequest) -> AsyncIterator[AgentEvent]:
        async for event in self.engine.run_stream(request):
            yield event

    async def resume_paused_stream(self, run_id: str) -> AsyncIterator[AgentEvent]:
        run = await self.runs.require(run_id, session_id=self.session_id)
        if run.status not in {"waiting_approval", "waiting_input"}:
            raise ValueError("run is not waiting for a resumable tool invocation")
        async for event in self.run_stream(
            RunRequest(
                question=run.question,
                resume_from_run_id=run.id,
                mode=RunMode.INTERACTIVE,
            )
        ):
            yield event


def _permission_approval_choice(
    value: ApprovalChoice | ApprovalDecision | str,
) -> ApprovalChoice:
    if value in {ApprovalDecision.APPROVE, ApprovalDecision.APPROVE.value, "approve"}:
        return ApprovalChoice.APPROVE_ONCE
    if value in {ApprovalDecision.REJECT, ApprovalDecision.REJECT.value}:
        return ApprovalChoice.REJECT
    try:
        return (
            value if isinstance(value, ApprovalChoice) else ApprovalChoice(str(value))
        )
    except ValueError as exc:
        raise ValueError("invalid permission approval choice") from exc


def _permission_update(raw: dict[str, Any], *, expected_tool: str):
    from ..tooling.permission_policy.models import (
        PermissionBehavior,
        PermissionDestination,
        PermissionUpdate,
        PermissionUpdateOperation,
    )

    update = PermissionUpdate(
        PermissionUpdateOperation(str(raw.get("operation"))),
        PermissionDestination(str(raw.get("destination"))),
        PermissionBehavior(str(raw.get("behavior"))),
        str(raw.get("tool")),
        dict(raw.get("constraints", {})),
        str(raw["rule_id"]) if raw.get("rule_id") else None,
    )
    if (
        update.operation is not PermissionUpdateOperation.ADD
        or update.behavior is not PermissionBehavior.ALLOW
        or update.destination
        not in {PermissionDestination.SESSION, PermissionDestination.LOCAL}
        or update.tool != expected_tool
    ):
        raise ValueError("unsafe permission update suggestion")
    return update


def _permission_update_for_management(raw: dict[str, Any]):
    from ..tooling.permission_policy.models import (
        PermissionBehavior,
        PermissionDestination,
        PermissionUpdate,
        PermissionUpdateOperation,
    )

    try:
        constraints = raw.get("constraints", {})
        if not isinstance(constraints, dict):
            raise ValueError("permission constraints must be an object")
        return PermissionUpdate(
            PermissionUpdateOperation(str(raw["operation"])),
            PermissionDestination(str(raw["destination"])),
            PermissionBehavior(str(raw.get("behavior", "allow"))),
            str(raw.get("tool", "*")),
            constraints,
            str(raw["rule_id"]) if raw.get("rule_id") else None,
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid permission update") from exc


def _permission_arguments_digest(arguments: dict[str, Any]) -> str:
    public = {
        key: value
        for key, value in arguments.items()
        if not key.startswith("_permission_")
    }
    encoded = json.dumps(
        public, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _permission_preview(arguments: dict[str, Any]) -> str:
    safe_keys = {
        "path",
        "cwd",
        "url",
        "query",
        "command",
        "server",
        "tool",
        "process_id",
        "name",
    }
    preview = redact(
        {key: value for key, value in arguments.items() if key in safe_keys}
    )
    text = json.dumps(preview, ensure_ascii=False, default=str)
    lines = text.splitlines()[:40]
    return "\n".join(lines).encode("utf-8")[:4096].decode("utf-8", errors="ignore")
