"""Validated, approval-gated command proposals and execution."""

from __future__ import annotations

import os
import json
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .policy import PolicyError, WorkspacePolicy
from .session import CommandInfo, SessionStore


@dataclass(frozen=True)
class CommandTemplate:
    name: str
    summary: str
    argv: tuple[str, ...]
    requires: str | None = None
    supports_target: bool = False


TEMPLATES = {
    "pytest": CommandTemplate("pytest", "Run the Python test suite", (sys.executable, "-m", "pytest"), "pyproject.toml", True),
    "npm_test": CommandTemplate("npm_test", "Run the npm test script", ("npm", "test"), "package.json"),
    "npm_build": CommandTemplate("npm_build", "Run the npm build script", ("npm", "run", "build"), "package.json"),
    "ruff_check": CommandTemplate("ruff_check", "Check Python style without modifying files", ("ruff", "check"), "pyproject.toml", True),
    "prettier_check": CommandTemplate("prettier_check", "Check formatting without modifying files", ("prettier", "--check"), "package.json", True),
}


class CommandService:
    def __init__(self, store: SessionStore, policy: WorkspacePolicy, session_id: str, run_id: str, emit: Callable[..., None], *, timeout_seconds: float = 120, output_limit_bytes: int = 100_000) -> None:
        self.store, self.policy, self.session_id, self.run_id, self.emit = store, policy, session_id, run_id, emit
        self.timeout_seconds, self.output_limit_bytes = timeout_seconds, output_limit_bytes

    def available_templates(self) -> list[str]:
        return [name for name, template in TEMPLATES.items() if self._project_supports(template)]

    def _project_supports(self, template: CommandTemplate) -> bool:
        if template.requires and not (self.policy.root / template.requires).is_file():
            return False
        if template.name not in {"npm_test", "npm_build"}:
            return True
        try:
            package = json.loads((self.policy.root / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        scripts = package.get("scripts", {})
        script = "test" if template.name == "npm_test" else "build"
        return isinstance(scripts, dict) and isinstance(scripts.get(script), str)

    def propose(self, template_name: str, *, target: str | None = None, cwd: str = ".") -> CommandInfo:
        template = TEMPLATES.get(template_name)
        if template is None or template_name not in self.available_templates():
            raise ValueError(f"unsupported command template: {template_name}")
        executable = template.argv[0] if Path(template.argv[0]).is_file() else shutil.which(template.argv[0])
        if executable is None:
            raise ValueError(f"required executable is unavailable: {template.argv[0]}")
        directory = self.policy.resolve(cwd)
        if not directory.is_dir() or any(part in {".git", ".capslock"} for part in directory.relative_to(self.policy.root).parts):
            raise PolicyError("command cwd must be a normal workspace directory")
        argv = list(template.argv)
        if target is not None:
            if not template.supports_target:
                raise ValueError(f"template does not accept a target: {template_name}")
            target_path = self.policy.resolve(target)
            if not target_path.is_relative_to(directory):
                raise PolicyError("command target must be inside its cwd")
            argv.append(str(target_path.relative_to(directory)))
        command = self.store.create_command(session_id=self.session_id, run_id=self.run_id, template=template.name, argv=argv, cwd=str(directory.relative_to(self.policy.root)), timeout_seconds=self.timeout_seconds, summary=template.summary)
        self.emit("command_proposed", command_id=command.id, template=command.template, argv=argv, cwd=command.cwd)
        return command

    def approve(self, command_id: str) -> CommandInfo:
        command = self._command(command_id)
        if command.status != "pending":
            raise ValueError(f"command is not pending: {command.status}")
        self.store.update_command(command.id, "approved")
        self.emit("command_approved", command_id=command.id)
        return self._command(command.id)

    def reject(self, command_id: str) -> CommandInfo:
        command = self._command(command_id)
        if command.status != "pending":
            raise ValueError(f"command is not pending: {command.status}")
        self.store.update_command(command.id, "rejected")
        self.emit("command_rejected", command_id=command.id)
        return self._command(command.id)

    def execute(self, command_id: str) -> CommandInfo:
        command = self._command(command_id)
        if command.status != "approved":
            raise ValueError("command requires explicit approval before execution")
        cwd = self.policy.resolve(command.cwd)
        self.store.update_command(command.id, "running")
        self.emit("command_started", command_id=command.id, argv=list(command.argv), cwd=command.cwd)
        try:
            process = subprocess.Popen(list(command.argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, shell=False, start_new_session=True)
        except OSError as exc:
            self.store.update_command(command.id, "failed", error=str(exc))
            self.emit("command_finished", command_id=command.id, status="failed", truncated=False)
            return self._command(command.id)
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired:
            self._stop(process)
            stdout_bytes, stderr_bytes = process.communicate()
            return self._finish(command, "failed", stdout_bytes, stderr_bytes, exit_code=process.returncode, error=f"command timed out after {command.timeout_seconds:g}s")
        except KeyboardInterrupt:
            self._stop(process)
            stdout_bytes, stderr_bytes = process.communicate()
            self._finish(command, "cancelled", stdout_bytes, stderr_bytes, exit_code=None, error="cancelled by user")
            raise
        status = "completed" if process.returncode == 0 else "failed"
        return self._finish(command, status, stdout_bytes, stderr_bytes, exit_code=process.returncode)

    def _finish(self, command: CommandInfo, status: str, stdout_bytes: bytes, stderr_bytes: bytes, *, exit_code: int | None, error: str | None = None) -> CommandInfo:
        output = stdout_bytes + stderr_bytes
        truncated = len(output) > self.output_limit_bytes
        stdout = stdout_bytes[: self.output_limit_bytes].decode("utf-8", errors="replace")
        remaining = max(0, self.output_limit_bytes - len(stdout_bytes))
        stderr = stderr_bytes[:remaining].decode("utf-8", errors="replace")
        self.store.update_command(command.id, status, exit_code=exit_code, stdout=stdout, stderr=stderr, truncated=truncated, error=error)
        self.emit("command_finished", command_id=command.id, status=status, truncated=truncated)
        return self._command(command.id)

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _command(self, command_id: str) -> CommandInfo:
        command = self.store.get_command(command_id, session_id=self.session_id)
        if command is None:
            raise PolicyError("command does not belong to this session or does not exist")
        return command
