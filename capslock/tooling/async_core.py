"""Async model-tool definitions and invocation registry."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..evidence import Evidence
from ..permissions import PermissionMode
from ..policy import PolicyError, WorkspacePolicy
from ..ports import ActionPort, MemoryPort, SkillPort, SourcePort, TaskPort
from .schema import SchemaValidationError, validate_json_schema


class InterruptBehavior(StrEnum):
    CANCEL = "cancel"
    COMPLETE = "complete"
    SHIELD = "shield"


@dataclass(frozen=True)
class ToolCapabilities:
    read_only: bool = False
    concurrency_safe: bool = False
    destructive: bool = False
    external_side_effects: bool = False
    context_mutation: bool = False
    required: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object] | None = None
    capabilities: ToolCapabilities = ToolCapabilities()
    interrupt_behavior: InterruptBehavior = InterruptBehavior.CANCEL
    inline_result_bytes: int = 16_384
    max_result_bytes: int = 5 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.name or self.inline_result_bytes <= 0:
            raise ValueError("invalid tool specification")
        if self.max_result_bytes < self.inline_result_bytes:
            raise ValueError("tool max result size must cover the inline limit")

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
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "capabilities": {
                "read_only": self.capabilities.read_only,
                "concurrency_safe": self.capabilities.concurrency_safe,
                "destructive": self.capabilities.destructive,
                "external_side_effects": self.capabilities.external_side_effects,
                "context_mutation": self.capabilities.context_mutation,
                "required": sorted(self.capabilities.required),
            },
            "interrupt_behavior": self.interrupt_behavior.value,
            "inline_result_bytes": self.inline_result_bytes,
            "max_result_bytes": self.max_result_bytes,
        }


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: object
    error: str | None = None
    citations: tuple[Evidence, ...] = ()
    source_ids: tuple[str, ...] = ()
    memories: tuple[object, ...] = ()
    audit_data: object | None = None
    audit_arguments: dict[str, object] | None = None
    event_data: dict[str, object] | None = None
    external_usage: dict[str, int | float] | None = None

    def for_model(self) -> str:
        return json.dumps(
            {"ok": self.ok, "data": self.data, "error": self.error},
            ensure_ascii=False,
            default=str,
        )

    def for_audit(self) -> str:
        data = self.data if self.audit_data is None else self.audit_data
        return json.dumps(
            {"ok": self.ok, "data": data, "error": self.error},
            ensure_ascii=False,
            default=str,
        )


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


ToolExecutor = Callable[[ExecutionContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, object]
    execute: ToolExecutor
    output_schema: dict[str, object] | None = None
    capabilities: ToolCapabilities = ToolCapabilities()
    interrupt_behavior: InterruptBehavior = InterruptBehavior.CANCEL
    inline_result_bytes: int = 16_384
    max_result_bytes: int = 5 * 1024 * 1024

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            self.name,
            self.description,
            self.parameters,
            self.output_schema,
            self.capabilities,
            self.interrupt_behavior,
            self.inline_result_bytes,
            self.max_result_bytes,
        )

    def schema(self) -> dict[str, object]:
        return self.spec.model_schema()


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    def combined(self, tools: list[Tool]) -> "ToolRegistry":
        return ToolRegistry([*self._tools.values(), *tools])

    @property
    def schemas(self) -> list[dict[str, object]]:
        return [tool.schema() for tool in self._tools.values()]

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def spec(self, name: str) -> ToolSpec | None:
        tool = self.get(name)
        return None if tool is None else tool.spec

    def filtered(self, names: set[str]) -> "ToolRegistry":
        return ToolRegistry(
            [tool for name, tool in self._tools.items() if name in names]
        )

    async def invoke(
        self, name: str, context: ExecutionContext, arguments: dict[str, Any]
    ) -> tuple[ToolResult, int]:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, {}, f"unsupported tool: {name}"), 0
        started = time.monotonic()
        context.event("tool_started", name=name)
        try:
            validate_json_schema(arguments, tool.parameters)
            result = await tool.execute(context, arguments)
            if tool.output_schema is not None:
                validate_json_schema(result.data, tool.output_schema)
        except (PolicyError, SchemaValidationError, ValueError, OSError) as exc:
            result = ToolResult(False, {}, str(exc))
        duration_ms = round((time.monotonic() - started) * 1000)
        context.event("tool_finished", name=name, ok=result.ok, duration_ms=duration_ms)
        return result, duration_ms
