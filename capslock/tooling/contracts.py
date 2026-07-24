"""Provider-neutral Tool Runtime contracts and construction helpers."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from ..evidence import Evidence
from ..permissions import PermissionMode
from ..policy import WorkspacePolicy
from ..ports import ActionPort, MemoryPort, SkillPort, SourcePort, TaskPort
from .schema import compile_json_schema


class InterruptBehavior(StrEnum):
    CANCEL = "cancel"
    COMPLETE = "complete"
    SHIELD = "shield"


class ToolOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    INLINE = "inline"
    ARTIFACT = "artifact"
    TRUNCATED = "truncated"
    DELIVERY_FAILED = "delivery_failed"


class ToolEventKind(StrEnum):
    PHASE = "phase"
    PROGRESS = "progress"
    USAGE = "usage"


@dataclass(frozen=True)
class ToolContent:
    """Provider-neutral model-facing content block."""

    kind: str
    value: object
    media_type: str | None = None

    @classmethod
    def text(cls, value: str) -> "ToolContent":
        return cls("text", value)

    @classmethod
    def json(cls, value: object) -> "ToolContent":
        return cls("json", value, "application/json")

    @classmethod
    def artifact(cls, value: dict[str, object]) -> "ToolContent":
        return cls("artifact", value)

    @classmethod
    def image(cls, value: object, media_type: str = "image/png") -> "ToolContent":
        return cls("image", value, media_type)

    @classmethod
    def resource(cls, value: object, media_type: str | None = None) -> "ToolContent":
        return cls("resource", value, media_type)

    def as_dict(self) -> dict[str, object]:
        return {
            "type": self.kind,
            "value": self.value,
            **({"media_type": self.media_type} if self.media_type else {}),
        }

    def summary_dict(self) -> dict[str, object]:
        if self.kind == "image":
            return {
                "type": "image",
                "embedded": True,
                **({"media_type": self.media_type} if self.media_type else {}),
            }
        return self.as_dict()


@dataclass(frozen=True)
class ToolEvent:
    kind: ToolEventKind
    data: dict[str, object]


ToolReporter = Callable[[ToolEvent], Awaitable[None]]


async def null_reporter(event: ToolEvent) -> None:
    del event


@dataclass(frozen=True)
class ResolvedToolPolicy:
    read_only: bool = False
    concurrency_safe: bool = False
    destructive: bool = False
    external_side_effects: bool = False
    open_world: bool = False
    context_mutation: bool = False
    interrupt_behavior: InterruptBehavior = InterruptBehavior.COMPLETE
    required_capabilities: frozenset[str] = frozenset()
    timeout_seconds: float | None = None
    fail_fast: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("tool timeout must be positive")
        if self.destructive and self.read_only:
            raise ValueError("a destructive tool cannot be read-only")
        if self.concurrency_safe and (
            not self.read_only
            or self.destructive
            or self.context_mutation
            or self.external_side_effects
        ):
            raise ValueError(
                "concurrency-safe tools must be local read-only operations"
            )

    @classmethod
    def safe_read(cls, *, timeout_seconds: float | None = None) -> "ResolvedToolPolicy":
        return cls(
            read_only=True,
            concurrency_safe=True,
            interrupt_behavior=InterruptBehavior.CANCEL,
            timeout_seconds=timeout_seconds,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "read_only": self.read_only,
            "concurrency_safe": self.concurrency_safe,
            "destructive": self.destructive,
            "external_side_effects": self.external_side_effects,
            "open_world": self.open_world,
            "context_mutation": self.context_mutation,
            "interrupt_behavior": self.interrupt_behavior.value,
            "required_capabilities": sorted(self.required_capabilities),
            "timeout_seconds": self.timeout_seconds,
            "fail_fast": self.fail_fast,
        }


@dataclass(frozen=True)
class ToolContract:
    name: str
    version: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object] | None = None
    search_hint: str | None = None
    deferred: bool = False
    inline_result_bytes: int = 16_384
    max_capture_bytes: int = 5 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.name or not self.version or not self.description.strip():
            raise ValueError("invalid tool contract")
        if self.inline_result_bytes <= 0:
            raise ValueError("inline tool result limit must be positive")
        if self.max_capture_bytes < self.inline_result_bytes:
            raise ValueError("tool capture limit must cover the inline limit")
        compile_json_schema(self.input_schema)
        if self.output_schema is not None:
            compile_json_schema(self.output_schema)

    def model_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def metadata(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "search_hint": self.search_hint,
            "deferred": self.deferred,
            "inline_result_bytes": self.inline_result_bytes,
            "max_capture_bytes": self.max_capture_bytes,
        }


@dataclass(frozen=True)
class ToolOutcome:
    status: ToolOutcomeStatus
    executed: bool
    data: object = field(default_factory=dict)
    content: tuple[ToolContent, ...] = ()
    error: str | None = None
    error_code: str | None = None
    citations: tuple[Evidence, ...] = ()
    source_ids: tuple[str, ...] = ()
    memories: tuple[object, ...] = ()
    audit_data: object | None = None
    audit_arguments: dict[str, object] | None = None
    event_data: dict[str, object] | None = None
    external_usage: dict[str, int | float] | None = None
    delivery_status: DeliveryStatus = DeliveryStatus.INLINE

    @property
    def ok(self) -> bool:
        return self.status is ToolOutcomeStatus.SUCCEEDED

    @classmethod
    def success(cls, data: object, **values: Any) -> "ToolOutcome":
        return cls(ToolOutcomeStatus.SUCCEEDED, True, data=data, **values)

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        code: str = "tool_failed",
        executed: bool = False,
        data: object | None = None,
    ) -> "ToolOutcome":
        return cls(
            ToolOutcomeStatus.FAILED,
            executed,
            {} if data is None else data,
            error=error,
            error_code=code,
        )

    def with_delivery(
        self, status: DeliveryStatus, content: ToolContent
    ) -> "ToolOutcome":
        return replace(self, delivery_status=status, content=(content,))

    def for_model(self) -> str:
        return json.dumps(
            {
                "status": self.status.value,
                "ok": self.ok,
                "executed": self.executed,
                "delivery_status": self.delivery_status.value,
                "data": self.data,
                "content": [item.summary_dict() for item in self.content],
                "error": self.error,
                "error_code": self.error_code,
            },
            ensure_ascii=False,
            default=str,
        )

    def for_audit(self) -> str:
        data = self.data if self.audit_data is None else self.audit_data
        return json.dumps(
            {
                "status": self.status.value,
                "executed": self.executed,
                "data": data,
                "error": self.error,
                "error_code": self.error_code,
            },
            ensure_ascii=False,
            default=str,
        )


@dataclass(frozen=True)
class ToolPause:
    """A durable, non-terminal suspension of one tool invocation."""

    kind: str
    request_id: str
    payload: dict[str, object]
    resume_data: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in {"approval", "user_input"}:
            raise ValueError(f"unsupported tool pause kind: {self.kind}")
        if not self.request_id:
            raise ValueError("tool pause request_id must not be empty")


ToolExecution = ToolOutcome | ToolPause


@dataclass(frozen=True, kw_only=True)
class ExecutionContext:
    session_id: str
    run_id: str
    policy: WorkspacePolicy
    event: Callable[..., None]
    actions: ActionPort
    tasks: TaskPort | None = None
    sources: SourcePort | None = None
    memory: MemoryPort | None = None
    skills: SkillPort | None = None
    permission_mode: PermissionMode = PermissionMode.APPROVE_FOR_ME
    collaboration: Any = None
    governor: Any = None
    artifacts: Any = None
    permission_engine: Any = None
    process_manager: Any = None
    invocation_id: str | None = None
    catalog: Any = None
    discoveries: Any = None
    runtime_state: dict[str, object] = field(default_factory=dict)
    shell_classifier: Any = None


ToolExecuteCallable = Callable[
    [ExecutionContext, dict[str, Any], ToolReporter], Awaitable[ToolExecution]
]
ToolResumer = Callable[
    [ExecutionContext, dict[str, Any], ToolPause, object, ToolReporter],
    Awaitable[ToolExecution],
]
ToolValidator = Callable[[dict[str, Any], ExecutionContext], Awaitable[None]]
ToolPolicyResolver = Callable[
    [dict[str, Any], ExecutionContext], Awaitable[ResolvedToolPolicy]
]
ToolPresenter = Callable[[dict[str, Any]], dict[str, object]]


async def _no_validation(arguments: dict[str, Any], context: ExecutionContext) -> None:
    del arguments, context


async def _default_policy(
    arguments: dict[str, Any], context: ExecutionContext
) -> ResolvedToolPolicy:
    del arguments, context
    return ResolvedToolPolicy()


@dataclass(frozen=True)
class ToolDefinition:
    contract: ToolContract
    execute: ToolExecuteCallable
    validate: ToolValidator = _no_validation
    resolve_policy: ToolPolicyResolver = _default_policy
    resume: ToolResumer | None = None
    presenter: ToolPresenter | None = None

    @property
    def name(self) -> str:
        return self.contract.name

    def schema(self) -> dict[str, object]:
        return self.contract.model_schema()


class ToolMiddleware(Protocol):
    async def normalize(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> dict[str, Any]: ...

    async def authorize(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        context: ExecutionContext,
    ) -> ToolExecution | None: ...

    async def after(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        outcome: ToolOutcome,
        context: ExecutionContext,
    ) -> ToolOutcome: ...


@dataclass(frozen=True)
class ToolInvocationResult:
    execution: ToolExecution
    arguments: dict[str, Any]
    policy: ResolvedToolPolicy
    timings_ms: dict[str, int]

    @property
    def outcome(self) -> ToolOutcome:
        if isinstance(self.execution, ToolPause):
            raise RuntimeError("paused tool execution has no terminal outcome")
        return self.execution


def adapt_executor(
    executor: Callable[..., Awaitable[ToolExecution]],
) -> ToolExecuteCallable:
    """Adapt internal two-argument executors while public v2 uses a reporter."""

    parameters = tuple(inspect.signature(executor).parameters.values())
    positional = tuple(
        item
        for item in parameters
        if item.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    )
    if len(positional) >= 3:
        return executor  # type: ignore[return-value]

    async def execute(
        context: ExecutionContext,
        arguments: dict[str, Any],
        reporter: ToolReporter,
    ) -> ToolExecution:
        del reporter
        return await executor(context, arguments)

    return execute


def define_tool(
    name: str,
    description: str,
    input_schema: dict[str, object],
    executor: Callable[..., Awaitable[ToolExecution]],
    *,
    version: str = "2.0.0",
    output_schema: dict[str, object] | None = None,
    policy: ResolvedToolPolicy | ToolPolicyResolver = ResolvedToolPolicy(),
    search_hint: str | None = None,
    deferred: bool = False,
    inline_result_bytes: int = 16_384,
    max_capture_bytes: int = 5 * 1024 * 1024,
    validate: ToolValidator = _no_validation,
    resume: ToolResumer | None = None,
    presenter: ToolPresenter | None = None,
) -> ToolDefinition:
    if isinstance(policy, ResolvedToolPolicy):
        resolved = policy

        async def policy_resolver(
            arguments: dict[str, Any], context: ExecutionContext
        ) -> ResolvedToolPolicy:
            del arguments, context
            return resolved

    else:
        policy_resolver = policy
    return ToolDefinition(
        ToolContract(
            name,
            version,
            description,
            input_schema,
            output_schema,
            search_hint,
            deferred,
            inline_result_bytes,
            max_capture_bytes,
        ),
        adapt_executor(executor),
        validate,
        policy_resolver,
        resume,
        presenter,
    )
