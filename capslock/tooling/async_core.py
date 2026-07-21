"""Async model-tool definitions and invocation registry."""

from __future__ import annotations

import json
import time
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..evidence import Evidence
from ..permissions import PermissionMode
from ..policy import PolicyError, WorkspacePolicy
from ..ports import ActionPort, MemoryPort, SkillPort, SourcePort, TaskPort


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
class RunContext:
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
    repositories: Any = None

    def __post_init__(self) -> None:
        if self.repositories is None:
            return
        warnings.warn(
            "RunContext(repositories=...) is deprecated; pass tasks= and sources=; "
            "removed in 2.0.0",
            DeprecationWarning,
            stacklevel=2,
        )
        if self.tasks is None:
            object.__setattr__(self, "tasks", self.repositories.tasks)
        if self.sources is None:
            object.__setattr__(self, "sources", self.repositories.sources)


ToolExecutor = Callable[[RunContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, object]
    execute: ToolExecutor

    def schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    @property
    def schemas(self) -> list[dict[str, object]]:
        return [tool.schema() for tool in self._tools.values()]

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    async def invoke(
        self, name: str, context: RunContext, arguments: dict[str, Any]
    ) -> tuple[ToolResult, int]:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, {}, f"unsupported tool: {name}"), 0
        started = time.monotonic()
        context.event("tool_started", name=name)
        try:
            result = await tool.execute(context, arguments)
        except (PolicyError, ValueError, OSError) as exc:
            result = ToolResult(False, {}, str(exc))
        duration_ms = round((time.monotonic() - started) * 1000)
        context.event("tool_finished", name=name, ok=result.ok, duration_ms=duration_ms)
        return result, duration_ms
