"""Shell direct-capability tool execution."""

from __future__ import annotations

import asyncio  # noqa: F401
import base64  # noqa: F401
import fnmatch  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import shutil  # noqa: F401
import uuid  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

from ...domain import ActionRecord, ActionStatus, ActionType  # noqa: F401
from ...evidence import Evidence  # noqa: F401
from ...security import TEXT_SUFFIXES  # noqa: F401
from ..contracts import (  # noqa: F401
    ExecutionContext,
    ToolContent,
    ToolExecution,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
)
from .actions import execute_action_tool  # noqa: F401
from .support import _outcome, _path  # noqa: F401


async def shell(context: ExecutionContext, arguments: dict[str, Any]) -> ToolExecution:
    return await execute_action_tool(context, ActionType.COMMAND, arguments)


async def process_output(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.process_manager is None:
        return ToolOutcome.failure(
            "background processes are unavailable", code="process_unavailable"
        )
    identifier = arguments.get("process_id")
    if not isinstance(identifier, str):
        return ToolOutcome.failure(
            "process_id must be a string", code="invalid_process_id"
        )
    job = context.process_manager.get(context.session_id, identifier)
    return ToolOutcome.success(
        {
            "process_id": job.id,
            "status": job.status,
            "exit_code": job.process.returncode,
            "stdout": bytes(job.stdout).decode("utf-8", errors="replace"),
            "stderr": bytes(job.stderr).decode("utf-8", errors="replace"),
            "truncated": len(job.stdout) >= job.output_limit
            or len(job.stderr) >= job.output_limit,
        }
    )


async def process_stop(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolOutcome:
    if context.process_manager is None:
        return ToolOutcome.failure(
            "background processes are unavailable", code="process_unavailable"
        )
    identifier = arguments.get("process_id")
    if not isinstance(identifier, str):
        return ToolOutcome.failure(
            "process_id must be a string", code="invalid_process_id"
        )
    job = await context.process_manager.stop(context.session_id, identifier)
    return ToolOutcome.success(
        {"process_id": job.id, "status": "stopped", "exit_code": job.process.returncode}
    )


def shell_tools():
    from ..contracts import InterruptBehavior, ResolvedToolPolicy, define_tool
    from .schemas import _schema, _str

    ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "shell",
            "Run a command through the durable approval workflow in an OS sandbox. The workspace is writable and network is disabled by default.",
            _schema(
                {
                    "command": _str(),
                    "cwd": _str(),
                    "timeout": {"type": "number", "exclusiveMinimum": 0},
                    "background": {"type": "boolean"},
                    "sandbox": {
                        "type": "string",
                        "enum": ["default"],
                    },
                    "network": {"type": "array", "items": _str(), "uniqueItems": True},
                },
                ["command"],
            ),
            shell,
            policy=_shell_policy,
        ),
        define_tool(
            "process_output",
            "Read bounded output and status for a background process in this session.",
            _schema({"process_id": _str()}, ["process_id"]),
            process_output,
            policy=ResolvedToolPolicy(
                read_only=True,
                interrupt_behavior=InterruptBehavior.CANCEL,
            ),
        ),
        define_tool(
            "process_stop",
            "Stop a background process in this session using SIGTERM then SIGKILL.",
            _schema({"process_id": _str()}, ["process_id"]),
            process_stop,
            policy=ResolvedToolPolicy(
                destructive=True,
                interrupt_behavior=InterruptBehavior.COMPLETE,
                required_capabilities=frozenset({"process"}),
            ),
        ),
    ]


__all__ = ["shell", "process_output", "process_stop", "shell_tools"]


async def _shell_policy(arguments, context):
    from ...shell import assess_shell
    from ..contracts import InterruptBehavior, ResolvedToolPolicy

    assessment = assess_shell(str(arguments.get("command", "")))
    network = arguments.get("network", [])
    if (
        assessment.behavior == "ask"
        and context.shell_classifier is not None
        and arguments.get("sandbox", "default") == "default"
        and not network
    ):
        classified = await context.shell_classifier.classify(
            command=str(arguments.get("command", "")),
            cwd=str(arguments.get("cwd", ".")),
            sandbox="default",
            parsed=assessment.parsed,
        )
        context.runtime_state["shell_classifier"] = classified.audit
        if classified.behavior == "allow":
            context.runtime_state["classifier_auto_allow"] = True
    return ResolvedToolPolicy(
        destructive=assessment.behavior == "deny",
        external_side_effects=True,
        open_world=bool(network),
        interrupt_behavior=InterruptBehavior.COMPLETE,
        required_capabilities=frozenset(
            {"process"} | ({"network"} if network else set())
        ),
        timeout_seconds=float(arguments.get("timeout", 120)) + 5,
    )
