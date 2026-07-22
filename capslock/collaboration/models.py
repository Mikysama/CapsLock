"""Typed contracts exchanged by the local collaboration runtime."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from ..security import redact


class CapabilityKind(StrEnum):
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    COMMAND = "command"
    WEB = "web"
    MCP = "mcp"
    PLUGIN = "plugin"


class AgentTaskState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class AgentMessageKind(StrEnum):
    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    MESSAGE_SENT = "message_sent"
    MESSAGE_RECEIVED = "message_received"
    TASK_CANCELLED = "task_cancelled"
    TASK_FINISHED = "task_finished"
    OUTPUT_VERIFIED = "output_verified"
    OUTPUT_REJECTED = "output_rejected"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"


@dataclass(frozen=True)
class CapabilityGrant:
    """Explicit child capability; omission means denial."""

    kind: CapabilityKind
    scope: str | None = None
    plugin: str | None = None

    def __post_init__(self) -> None:
        if self.kind is CapabilityKind.PLUGIN and not self.plugin:
            raise ValueError("plugin capability requires a plugin name")
        if self.kind is not CapabilityKind.PLUGIN and self.plugin is not None:
            raise ValueError("plugin is only valid for plugin capability")
        if self.scope is not None and not self.scope.strip():
            raise ValueError("capability scope cannot be empty")

    def as_dict(self) -> dict[str, str]:
        result = {"kind": self.kind.value}
        if self.scope is not None:
            result["scope"] = self.scope
        if self.plugin is not None:
            result["plugin"] = self.plugin
        return result


@dataclass(frozen=True)
class VerificationRequirement:
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    required_paths: tuple[str, ...] = ()
    max_artifacts: int = 20
    max_artifact_bytes: int = 512_000
    required_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_artifacts < 0 or self.max_artifacts > 100:
            raise ValueError("max_artifacts must be between 0 and 100")
        if self.max_artifact_bytes < 1 or self.max_artifact_bytes > 10_000_000:
            raise ValueError("max_artifact_bytes is outside the supported range")

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_schema": dict(self.output_schema),
            "required_paths": list(self.required_paths),
            "max_artifacts": self.max_artifacts,
            "max_artifact_bytes": self.max_artifact_bytes,
            "required_checks": list(self.required_checks),
        }


@dataclass(frozen=True)
class AgentTaskContract:
    task_id: str
    parent_run_id: str
    objective: str
    input_context: Mapping[str, Any] = field(default_factory=dict)
    allowed_paths: tuple[str, ...] = ()
    capabilities: tuple[CapabilityGrant, ...] = ()
    model_profile: str | None = None
    limits: Mapping[str, int | float | None] = field(default_factory=dict)
    verification_requirements: VerificationRequirement = field(
        default_factory=VerificationRequirement
    )

    def __post_init__(self) -> None:
        if not self.task_id or not self.parent_run_id:
            raise ValueError("task_id and parent_run_id are required")
        if not self.objective.strip():
            raise ValueError("task objective must not be empty")
        if len(self.objective) > 20_000:
            raise ValueError("task objective is too large")
        if redact(dict(self.input_context)) != dict(self.input_context):
            raise ValueError("task input context contains secret-like fields")
        if len(self.allowed_paths) > 100:
            raise ValueError("too many allowed paths")
        for path in self.allowed_paths:
            candidate = Path(path)
            if (
                not path
                or candidate.is_absolute()
                or ".." in candidate.parts
                or any(part in {".git", ".capslock"} for part in candidate.parts)
                or any(
                    part == ".env" or part.startswith(".env.")
                    for part in candidate.parts
                )
            ):
                raise ValueError(f"invalid child allowed path: {path}")
        if len(self.capabilities) > 32:
            raise ValueError("too many capability grants")
        for key, value in self.limits.items():
            if key not in {
                "max_tool_rounds",
                "max_tool_calls",
                "max_duration_ms",
                "max_tokens",
                "max_budget_usd",
            }:
                raise ValueError(f"unsupported task limit: {key}")
            if value is not None and float(value) <= 0:
                raise ValueError(f"task limit must be positive: {key}")

    @classmethod
    def create(
        cls,
        parent_run_id: str,
        objective: str,
        *,
        task_id: str | None = None,
        input_context: Mapping[str, Any] | None = None,
        allowed_paths: tuple[str, ...] = (),
        capabilities: tuple[CapabilityGrant, ...] = (),
        model_profile: str | None = None,
        limits: Mapping[str, int | float | None] | None = None,
        verification_requirements: VerificationRequirement | None = None,
    ) -> "AgentTaskContract":
        return cls(
            task_id=task_id or uuid.uuid4().hex,
            parent_run_id=parent_run_id,
            objective=objective,
            input_context=input_context or {},
            allowed_paths=allowed_paths,
            capabilities=capabilities,
            model_profile=model_profile,
            limits=limits or {"max_tool_rounds": 16},
            verification_requirements=verification_requirements
            or VerificationRequirement(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parent_run_id": self.parent_run_id,
            "objective": self.objective,
            "input_context": dict(self.input_context),
            "allowed_paths": list(self.allowed_paths),
            "capabilities": [item.as_dict() for item in self.capabilities],
            "model_profile": self.model_profile,
            "limits": dict(self.limits),
            "verification_requirements": self.verification_requirements.as_dict(),
        }

    def digest(self) -> str:
        encoded = json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentMessage:
    message_id: str
    task_id: str
    parent_run_id: str
    sender: str
    recipient: str
    sequence: int
    kind: AgentMessageKind
    payload: Mapping[str, Any]
    created_at: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("message sequence must be positive")
        if not self.sender or not self.recipient:
            raise ValueError("message sender and recipient are required")

    @property
    def safe_payload(self) -> dict[str, Any]:
        value = redact(dict(self.payload))
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > 32_000:
            return {"truncated": True, "sha256": self.payload_digest}
        return value

    @property
    def payload_digest(self) -> str:
        encoded = json.dumps(
            redact(dict(self.payload)), sort_keys=True, ensure_ascii=False, default=str
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidatedAgentOutput:
    task_id: str
    state: AgentTaskState
    summary: str
    evidence: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    checks: tuple[dict[str, Any], ...] = ()
    usage: Mapping[str, int | float] = field(default_factory=dict)
    verified: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state.value,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "artifacts": list(self.artifacts),
            "checks": list(self.checks),
            "usage": dict(self.usage),
            "verified": self.verified,
            "error": self.error,
        }
