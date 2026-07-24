"""Async fixed-template command handler with process-group cancellation."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain import ActionRecord, ActionResultKind, ActionType
from ...policy import PolicyError, WorkspacePolicy
from ...shell import (
    SandboxedCommand,
    SessionProcessManager,
    assess_shell,
    sandboxed_command,
    stop_process,
)
from .core import ActionExecution, ActionProposal


@dataclass(frozen=True)
class CommandTemplate:
    name: str
    summary: str
    argv: tuple[str, ...]
    requires: str | None = None
    supports_target: bool = False


TEMPLATES = {
    "pytest": CommandTemplate(
        "pytest",
        "Run the Python test suite",
        (sys.executable, "-m", "pytest"),
        "pyproject.toml",
        True,
    ),
    "npm_test": CommandTemplate(
        "npm_test", "Run the npm test script", ("npm", "test"), "package.json"
    ),
    "npm_build": CommandTemplate(
        "npm_build", "Run the npm build script", ("npm", "run", "build"), "package.json"
    ),
    "ruff_check": CommandTemplate(
        "ruff_check",
        "Check Python style without modifying files",
        ("ruff", "check"),
        "pyproject.toml",
        True,
    ),
    "prettier_check": CommandTemplate(
        "prettier_check",
        "Check formatting without modifying files",
        ("prettier", "--check"),
        "package.json",
        True,
    ),
}


class CommandActionHandler:
    types = frozenset({ActionType.COMMAND})

    def __init__(
        self,
        policy: WorkspacePolicy,
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
        max_timeout_seconds: float = 600,
        process_manager: SessionProcessManager | None = None,
    ) -> None:
        self.policy = policy
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes
        self.max_timeout_seconds = max_timeout_seconds
        self.process_manager = process_manager

    async def propose(
        self, action_type: ActionType, payload: dict[str, Any]
    ) -> ActionProposal:
        if "command" in payload:
            return await self._propose_shell(payload)
        template_name = payload.get("template")
        target, cwd = payload.get("target"), payload.get("cwd", ".")
        if (
            not isinstance(template_name, str)
            or target is not None
            and not isinstance(target, str)
            or not isinstance(cwd, str)
        ):
            raise ValueError("template, target, and cwd must be strings")
        template = TEMPLATES.get(template_name)
        if template is None or not self._project_supports(template):
            raise ValueError(f"unsupported command template: {template_name}")
        executable = (
            template.argv[0]
            if Path(template.argv[0]).is_file()
            else shutil.which(template.argv[0])
        )
        if executable is None:
            raise ValueError(f"required executable is unavailable: {template.argv[0]}")
        directory = self.policy.command_directory(cwd)
        argv = list(template.argv)
        if target is not None:
            if not template.supports_target:
                raise ValueError(f"template does not accept a target: {template_name}")
            target_path = self.policy.resolve(target)
            if not target_path.is_relative_to(directory):
                raise PolicyError("command target must be inside its cwd")
            argv.append(str(target_path.relative_to(directory)))
        return ActionProposal(
            template.summary,
            {
                "template": template.name,
                "argv": argv,
                "cwd": str(directory.relative_to(self.policy.root)),
                "timeout_seconds": self.timeout_seconds,
            },
        )

    async def _propose_shell(self, payload: dict[str, Any]) -> ActionProposal:
        command = payload.get("command")
        cwd = payload.get("cwd", ".")
        sandbox = payload.get("sandbox", "default")
        network = payload.get("network", [])
        background = payload.get("background", False)
        timeout = payload.get("timeout", self.timeout_seconds)
        if not isinstance(command, str) or not isinstance(cwd, str):
            raise ValueError("shell command and cwd must be strings")
        if sandbox != "default":
            raise PolicyError("only the default OS sandbox is supported")
        if not isinstance(network, list) or not all(
            isinstance(item, str) for item in network
        ):
            raise ValueError("shell network must be an array of host scopes")
        if not isinstance(background, bool):
            raise ValueError("shell background must be a boolean")
        timeout = float(timeout)
        if timeout <= 0 or timeout > self.max_timeout_seconds:
            raise ValueError(
                f"shell timeout must be between 0 and {self.max_timeout_seconds:g} seconds"
            )
        assessment = assess_shell(command)
        if assessment.behavior == "deny":
            raise PolicyError(assessment.reason)
        directory = self.policy.command_directory(cwd)
        built = sandboxed_command(
            command=command,
            workspace=self.policy.root,
            cwd=directory,
            network=network,
        )
        force_approval = bool(
            payload.get("force_manual_approval")
            or assessment.behavior == "ask"
            or network
            or background
        )
        return ActionProposal(
            f"Run sandboxed shell command: {command[:160]}",
            {
                "command": command,
                "argv": list(built.argv),
                "cwd": str(directory.relative_to(self.policy.root)),
                "sandbox": "default",
                "network": network,
                "background": background,
                "timeout_seconds": timeout,
                "temporary": str(built.temporary),
                "safety": {
                    "behavior": assessment.behavior,
                    "reason": assessment.reason,
                    "parsed": list(assessment.parsed),
                },
                "force_manual_approval": force_approval,
            },
        )

    def _project_supports(self, template: CommandTemplate) -> bool:
        if template.requires and not (self.policy.root / template.requires).is_file():
            return False
        if template.name not in {"npm_test", "npm_build"}:
            return True
        try:
            package = json.loads(
                (self.policy.root / "package.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return False
        scripts = package.get("scripts", {})
        script = "test" if template.name == "npm_test" else "build"
        return isinstance(scripts, dict) and isinstance(scripts.get(script), str)

    async def revalidate(self, action: ActionRecord) -> ActionProposal:
        request = action.request
        if "command" in request:
            return await self._propose_shell(
                {
                    "command": request.get("command"),
                    "cwd": request.get("cwd", "."),
                    "sandbox": request.get("sandbox", "default"),
                    "network": request.get("network", []),
                    "background": request.get("background", False),
                    "timeout": request.get("timeout_seconds", self.timeout_seconds),
                    "force_manual_approval": request.get(
                        "force_manual_approval", False
                    ),
                }
            )
        name = str(request.get("template", ""))
        template = TEMPLATES.get(name)
        argv = request.get("argv", [])
        target = None
        if template is not None and isinstance(argv, list):
            expected = len(template.argv)
            if template.supports_target and len(argv) == expected + 1:
                target = argv[expected]
        return await self.propose(
            action.type,
            {"template": name, "target": target, "cwd": request.get("cwd", ".")},
        )

    async def execute(self, action: ActionRecord) -> ActionExecution:
        request = action.request
        if request.get("background") is True:
            if self.process_manager is None:
                raise RuntimeError("background process manager is unavailable")
            job = await self.process_manager.start(
                action.session_id,
                SandboxedCommand(
                    tuple(str(item) for item in request["argv"]),
                    self.policy.root,
                    Path(str(request["temporary"])),
                ),
            )
            return ActionExecution(
                {
                    "process_id": job.id,
                    "status": job.status,
                    "stdout": "",
                    "stderr": "",
                    "truncated": False,
                    "timed_out": False,
                },
                ActionResultKind.EXIT_ZERO,
            )
        process = await asyncio.create_subprocess_exec(
            *[str(item) for item in request["argv"]],
            cwd=self.policy.resolve(str(request["cwd"])),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(float(request["timeout_seconds"])):
                stdout_bytes, stderr_bytes = await process.communicate()
        except TimeoutError:
            await self._stop(process)
            stdout_bytes, stderr_bytes = await process.communicate()
            return self._result(
                process.returncode, stdout_bytes, stderr_bytes, timed_out=True
            )
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._cancel_process(process))
            await _await_cleanup(cleanup)
            raise
        finally:
            temporary = request.get("temporary")
            if isinstance(temporary, str):
                shutil.rmtree(temporary, ignore_errors=True)
        return self._result(process.returncode, stdout_bytes, stderr_bytes)

    def _result(
        self,
        exit_code: int | None,
        stdout_bytes: bytes,
        stderr_bytes: bytes,
        *,
        timed_out: bool = False,
    ) -> ActionExecution:
        truncated = len(stdout_bytes) + len(stderr_bytes) > self.output_limit_bytes
        stdout = stdout_bytes[: self.output_limit_bytes].decode(
            "utf-8", errors="replace"
        )
        remaining = max(0, self.output_limit_bytes - len(stdout_bytes))
        stderr = stderr_bytes[:remaining].decode("utf-8", errors="replace")
        result = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
            "timed_out": timed_out,
        }
        if timed_out:
            return ActionExecution(result, ActionResultKind.TIMEOUT)
        kind = (
            ActionResultKind.EXIT_ZERO
            if exit_code == 0
            else ActionResultKind.NONZERO_EXIT
        )
        return ActionExecution(result, kind)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        await stop_process(process)

    async def _cancel_process(self, process: asyncio.subprocess.Process) -> None:
        await self._stop(process)
        await process.communicate()

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("command actions cannot be reversed")


async def _await_cleanup(task: asyncio.Task) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    await task
