"""Async fixed-template command handler with process-group cancellation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain import ActionRecord, ActionResultKind, ActionType
from ...policy import PolicyError, WorkspacePolicy
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
    ) -> None:
        self.policy = policy
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes

    async def propose(
        self, action_type: ActionType, payload: dict[str, Any]
    ) -> ActionProposal:
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

    async def execute(self, action: ActionRecord) -> ActionExecution:
        request = action.request
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
            await self._stop(process)
            await process.communicate()
            raise
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
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def reverse(self, action: ActionRecord) -> dict[str, Any]:
        raise ValueError("command actions cannot be reversed")
