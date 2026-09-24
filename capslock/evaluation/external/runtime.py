"""Run the production CapsLock CLI in an isolated benchmark workspace."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ModelTrack


@dataclass(frozen=True)
class RuntimeLimits:
    max_tool_rounds: int
    max_tool_calls: int
    max_tokens: int
    max_duration_seconds: int


@dataclass(frozen=True)
class RolloutOutcome:
    terminal_status: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    tool_rounds: int
    tool_calls: int
    returncode: int
    infrastructure_error: str | None = None
    peak_context_tokens: int = 0
    context_updates: int = 0
    context_compactions: int = 0


class CapsLockRuntime:
    def __init__(
        self,
        *,
        executable: Path,
        track: ModelTrack,
        limits: RuntimeLimits,
        prompt_template: str,
    ) -> None:
        self.executable = executable
        self.track = track
        self.limits = limits
        self.prompt_template = prompt_template

    def run(
        self,
        *,
        workspace: Path,
        state_home: Path,
        problem_statement: str,
        trajectory_path: Path,
        stderr_path: Path,
    ) -> RolloutOutcome:
        source_credential = os.environ.get(self.track.credential_env)
        if not source_credential:
            raise RuntimeError(f"missing model credential: {self.track.credential_env}")
        self._replace_project_state(workspace)
        state_home.mkdir(parents=True, exist_ok=False)
        environment = self._environment(state_home, source_credential)
        initialize = [
            str(self.executable),
            "--workspace",
            str(workspace),
            "init",
            "--non-interactive",
            "--provider",
            self.track.provider,
            "--base-url",
            self.track.base_url,
            "--model",
            self.track.model,
            "--credential",
            "env:CAPSLOCK_EVAL_API_KEY",
            "--permission-mode",
            "full_access",
            "--disable-memory",
        ]
        initialized = subprocess.run(
            initialize,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if initialized.returncode:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            _write_redacted(
                stderr_path,
                initialized.stdout + initialized.stderr,
                source_credential,
            )
            return RolloutOutcome(
                "infrastructure_error",
                "initialization_failed",
                0,
                0,
                0,
                0,
                0,
                initialized.returncode,
                "CapsLock benchmark workspace initialization failed",
            )
        self._harden_generated_config(workspace, self.track)
        prompt = self.prompt_template.replace(
            "{problem_statement}", problem_statement.strip()
        )
        command = [
            str(self.executable),
            "--workspace",
            str(workspace),
            "exec",
            "--json",
            "--quiet",
            "--no-memory",
            "--max-tool-rounds",
            str(self.limits.max_tool_rounds),
            "--max-tool-calls",
            str(self.limits.max_tool_calls),
            "--max-tokens",
            str(self.limits.max_tokens),
            "--max-duration-seconds",
            str(self.limits.max_duration_seconds),
            prompt,
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                timeout=self.limits.max_duration_seconds + 30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            _write_redacted(trajectory_path, exc.stdout or "", source_credential)
            _write_redacted(stderr_path, exc.stderr or "", source_credential)
            return RolloutOutcome(
                "stopped",
                "max_duration",
                0,
                0,
                duration,
                0,
                0,
                4,
            )
        duration = time.monotonic() - started
        _write_redacted(trajectory_path, completed.stdout, source_credential)
        _write_redacted(stderr_path, completed.stderr, source_credential)
        return _parse_outcome(completed.stdout, completed.returncode, duration)

    def _environment(self, state_home: Path, credential: str) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(
                marker in key.casefold()
                for marker in (
                    "api_key",
                    "authorization",
                    "password",
                    "secret",
                    "token",
                )
            )
        }
        environment.update(
            {
                "CAPSLOCK_HOME": str(state_home.resolve()),
                "CAPSLOCK_EVAL_API_KEY": credential,
                "CAPSLOCK_NO_SPINNER": "1",
                "CI": "true",
            }
        )
        return environment

    @staticmethod
    def _replace_project_state(workspace: Path) -> None:
        state = workspace / ".capslock"
        if state.exists():
            if state.is_symlink():
                state.unlink()
            elif state.is_dir():
                shutil.rmtree(state)
            else:
                state.unlink()

    @staticmethod
    def _harden_generated_config(workspace: Path, track: ModelTrack) -> None:
        import tomlkit

        path = workspace / ".capslock" / "config.toml"
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        document["agents"] = {
            "enabled": False,
            "background_enabled": False,
            "mailbox_enabled": False,
            "message_ttl_seconds": 3600,
        }
        document["mcp"] = {"remote_enabled": False}
        document["worktree"] = {"enabled": False, "max_per_session": 1}
        document["memory"] = {
            "capture_enabled": False,
            "recall_enabled": False,
            "manual_write_enabled": False,
            "maintenance_enabled": False,
            "policy": "off",
        }
        models = document.get("models")
        if isinstance(models, dict):
            for profile in models.values():
                if isinstance(profile, dict):
                    profile["context_window"] = track.context_window
                    profile["max_output_tokens"] = track.max_output_tokens
        path.write_text(tomlkit.dumps(document), encoding="utf-8")


def create_runtime_environment(wheel: Path, destination: Path) -> Path:
    """Install one immutable CapsLock wheel for a complete evaluation batch."""
    if not wheel.is_file():
        raise FileNotFoundError(f"CapsLock wheel does not exist: {wheel}")
    if destination.exists():
        shutil.rmtree(destination)
    subprocess.run([sys.executable, "-m", "venv", str(destination)], check=True)
    executable_dir = "Scripts" if os.name == "nt" else "bin"
    python = (
        destination / executable_dir / ("python.exe" if os.name == "nt" else "python")
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            str(wheel.resolve()),
        ],
        check=True,
    )
    executable = (
        destination
        / executable_dir
        / ("capslock.exe" if os.name == "nt" else "capslock")
    )
    if not executable.is_file():
        raise RuntimeError("wheel installation did not create the capslock executable")
    return executable


def _write_redacted(path: Path, value: str | bytes, secret: str) -> None:
    text = value.decode(errors="replace") if isinstance(value, bytes) else value
    if secret:
        text = text.replace(secret, "<redacted>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _parse_outcome(output: str, returncode: int, duration: float) -> RolloutOutcome:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    terminal = next(
        (event for event in reversed(events) if event.get("terminal")), None
    )
    if terminal is None:
        return RolloutOutcome(
            "infrastructure_error",
            "missing_terminal_event",
            0,
            0,
            duration,
            0,
            0,
            returncode,
            "CapsLock JSONL did not contain a terminal event",
        )
    context_events = [
        event for event in events if event.get("event") == "context_updated"
    ]
    peak_context_tokens = max(
        (
            int(((event.get("data") or {}).get("context") or {}).get("used_tokens", 0))
            for event in context_events
        ),
        default=0,
    )
    context_compactions = sum(
        1 for event in context_events if (event.get("data") or {}).get("compaction")
    )
    data = terminal.get("data") or {}
    usage = data.get("usage") or {}
    governance = data.get("governance") or data.get("limits") or {}
    budget = data.get("budget") or {}
    used = budget.get("used") if isinstance(budget, dict) else {}
    if not isinstance(used, dict):
        used = {}
    status = str(terminal.get("status") or terminal.get("event") or "failed")
    stop_reason = data.get("stop_reason") or terminal.get("stop_reason")
    return RolloutOutcome(
        status,
        str(stop_reason) if stop_reason is not None else None,
        int(usage.get("input_tokens", usage.get("prompt_tokens", 0))),
        int(usage.get("output_tokens", usage.get("completion_tokens", 0))),
        duration,
        int(
            governance.get(
                "tool_rounds", data.get("tool_rounds", used.get("tool_rounds", 0))
            )
        ),
        int(
            governance.get(
                "tool_calls", data.get("tool_calls", used.get("tool_calls", 0))
            )
        ),
        returncode,
        None,
        peak_context_tokens,
        len(context_events),
        context_compactions,
    )
