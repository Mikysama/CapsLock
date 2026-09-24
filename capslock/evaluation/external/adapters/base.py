"""Common adapter boundary without benchmark-specific grading logic."""

from __future__ import annotations

import json
import os
import hashlib
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts import ArtifactKind, ExternalTask, SuiteDefinition, file_sha256

if TYPE_CHECKING:
    from ..runtime import CapsLockRuntime, RolloutOutcome


_RUNTIME_GENERATED_DIRS = (
    ".venv",
    "venv",
    ".tox",
    ".pytest_cache",
    ".mypy_cache",
    "__pycache__",
    ".eggs",
)
_RUNTIME_GENERATED_DIR_SET = frozenset(_RUNTIME_GENERATED_DIRS)
# Preserve tracked source even when its directory resembles generated output.
# Untracked cache/virtualenv files are filtered separately; build/dist use Git ignore.
_PATCH_EXCLUDES = (":(exclude).capslock/**",)


@dataclass(frozen=True)
class AdapterContext:
    suite: SuiteDefinition
    harness_root: Path
    run_root: Path
    workspace: Path
    run_id: str
    ordinal: int


@dataclass(frozen=True)
class Artifact:
    kind: ArtifactKind
    path: Path
    sha256: str
    changed_files: int = 0
    changed_lines: int = 0


@dataclass(frozen=True)
class GradeOutcome:
    passed: bool
    infrastructure_error: str | None
    log_path: Path
    raw: dict[str, Any]


class OfficialAdapter:
    """Execute official commands supplied by a pinned, trusted task catalog."""

    id = "base"
    accepted_grade_prefixes: tuple[tuple[str, ...], ...] = ()

    def __init__(self, definition: SuiteDefinition) -> None:
        self.definition = definition

    def doctor(self, harness_root: Path) -> list[str]:
        failures = []
        probe = self.definition.harness_probe
        executable = probe[0]
        local = harness_root / executable
        if shutil.which(executable) is None and not local.is_file():
            failures.append(f"missing harness command: {executable}")
        else:
            command = [str(local) if local.is_file() else executable, *probe[1:]]
            try:
                completed = subprocess.run(
                    command,
                    cwd=harness_root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if completed.returncode:
                    detail = (completed.stderr or completed.stdout).strip()[-500:]
                    failures.append(
                        f"harness probe failed ({completed.returncode}): {detail}"
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"harness probe failed: {type(exc).__name__}: {exc}")
        if self.definition.resource_classes and shutil.which("docker") is None:
            failures.append("missing Docker CLI")
        if any("gpu" in item for item in self.definition.resource_classes):
            if shutil.which("modal") is None:
                failures.append("missing Modal CLI for GPU resource class")
        return failures

    def prepare(self, task: ExternalTask, destination: Path) -> Path:
        source = Path(task.workspace_source).expanduser().resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"task workspace does not exist: {source}")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, symlinks=True)
        return destination

    def launch(
        self,
        task: ExternalTask,
        runtime: CapsLockRuntime,
        *,
        workspace: Path,
        state_home: Path,
        trajectory_path: Path,
        stderr_path: Path,
    ) -> RolloutOutcome:
        return runtime.run(
            workspace=workspace,
            state_home=state_home,
            problem_statement=task.problem_statement,
            trajectory_path=trajectory_path,
            stderr_path=stderr_path,
        )

    def collect(self, task: ExternalTask, workspace: Path, output: Path) -> Artifact:
        if self.definition.artifact_kind is ArtifactKind.GIT_PATCH:
            return self._collect_patch(workspace, output)
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "instance_id": task.instance_id,
            "state": "ready_for_official_grader",
            "tree": _workspace_tree(workspace),
        }
        output.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        return Artifact(ArtifactKind.ENVIRONMENT_STATE, output, file_sha256(output))

    def grade(
        self,
        task: ExternalTask,
        artifact: Artifact,
        context: AdapterContext,
    ) -> GradeOutcome:
        raw_command = task.grader.get("command")
        if not isinstance(raw_command, list) or not raw_command:
            raise ValueError(f"task {task.instance_id} has no official grader command")
        command = [
            self._expand(str(value), task, artifact, context) for value in raw_command
        ]
        self._validate_grade_command(command)
        cwd_value = str(task.grader.get("cwd", "harness"))
        cwd = artifact.path.parent if cwd_value == "artifact" else context.harness_root
        if cwd_value == "workspace":
            cwd = context.workspace
        environment = os.environ.copy()
        supplied = task.grader.get("environment") or {}
        if not isinstance(supplied, dict):
            raise ValueError("grader environment must be an object")
        for key, value in supplied.items():
            if not str(key).startswith("CAPSLOCK_EVAL_"):
                raise ValueError(
                    "grader environment keys must use CAPSLOCK_EVAL_ prefix"
                )
            environment[str(key)] = self._expand(str(value), task, artifact, context)
        command, temporary_files = self._prepare_grade_command(
            command, task, artifact, context
        )
        log_path = artifact.path.parent / "grader.log"
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=float(task.grader.get("timeout_seconds", 3600)),
                check=False,
            )
            log_path.write_text(
                _redact_environment(completed.stdout + completed.stderr, environment),
                encoding="utf-8",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            return GradeOutcome(False, f"{type(exc).__name__}: {exc}", log_path, {})
        finally:
            for path in temporary_files:
                path.unlink(missing_ok=True)
        passed = self.normalise_grade(task, completed.returncode, log_path)
        return GradeOutcome(
            passed,
            None,
            log_path,
            {"returncode": completed.returncode, "command": shlex.join(command)},
        )

    def normalise_grade(
        self, task: ExternalTask, returncode: int, log_path: Path
    ) -> bool:
        expected = int(task.grader.get("success_exit_code", 0))
        required = task.grader.get("success_substring")
        if returncode != expected:
            return False
        return required is None or str(required) in log_path.read_text(
            encoding="utf-8", errors="replace"
        )

    def cleanup(self, workspace: Path) -> None:
        if workspace.exists():
            shutil.rmtree(workspace)

    def _validate_grade_command(self, command: list[str]) -> None:
        if not any(
            tuple(command[: len(prefix)]) == prefix
            for prefix in self.accepted_grade_prefixes
        ):
            allowed = ", ".join(
                shlex.join(prefix) for prefix in self.accepted_grade_prefixes
            )
            raise ValueError(
                f"grader command is not an accepted {self.id} harness command; expected {allowed}"
            )

    def _prepare_grade_command(
        self,
        command: list[str],
        task: ExternalTask,
        artifact: Artifact,
        context: AdapterContext,
    ) -> tuple[list[str], tuple[Path, ...]]:
        del task, artifact, context
        return command, ()

    @staticmethod
    def _expand(
        value: str, task: ExternalTask, artifact: Artifact, context: AdapterContext
    ) -> str:
        replacements = {
            "{artifact}": str(artifact.path.resolve()),
            "{harness_root}": str(context.harness_root.resolve()),
            "{instance_id}": task.instance_id,
            "{run_id}": context.run_id,
            "{run_root}": str(context.run_root.resolve()),
            "{workspace}": str(context.workspace.resolve()),
        }
        for marker, replacement in replacements.items():
            value = value.replace(marker, replacement)
        if "{" in value or "}" in value:
            raise ValueError(f"unknown grader command placeholder: {value}")
        return value

    @staticmethod
    def _collect_patch(workspace: Path, output: Path) -> Artifact:
        if not (workspace / ".git").exists():
            raise RuntimeError("patch benchmark workspace is not a Git checkout")
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=workspace,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        untracked = [
            value.decode(errors="surrogateescape") for value in untracked if value
        ]
        untracked = [value for value in untracked if not _is_runtime_generated(value)]
        if untracked:
            subprocess.run(
                ["git", "add", "--intent-to-add", "--", *untracked],
                cwd=workspace,
                capture_output=True,
                check=True,
            )
        completed = subprocess.run(
            [
                "git",
                "diff",
                "HEAD",
                "--binary",
                "--",
                ".",
                *_PATCH_EXCLUDES,
            ],
            cwd=workspace,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.decode(errors="replace"))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(completed.stdout)
        stats = subprocess.run(
            [
                "git",
                "diff",
                "HEAD",
                "--numstat",
                "--",
                ".",
                *_PATCH_EXCLUDES,
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        changed_files = changed_lines = 0
        for line in stats.stdout.splitlines():
            added, removed, _ = line.split("\t", 2)
            changed_files += 1
            if added.isdigit():
                changed_lines += int(added)
            if removed.isdigit():
                changed_lines += int(removed)
        return Artifact(
            ArtifactKind.GIT_PATCH,
            output,
            file_sha256(output),
            changed_files,
            changed_lines,
        )


def _is_runtime_generated(path: str) -> bool:
    return any(part in _RUNTIME_GENERATED_DIR_SET for part in Path(path).parts)


def _workspace_tree(workspace: Path) -> list[dict[str, object]]:
    entries = []
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if relative.parts and relative.parts[0] in {".capslock", ".git"}:
            continue
        metadata = path.lstat()
        if path.is_symlink():
            target = os.readlink(path)
            entries.append(
                {
                    "path": relative.as_posix(),
                    "type": "symlink",
                    "target": target,
                    "sha256": hashlib.sha256(target.encode()).hexdigest(),
                }
            )
        elif path.is_file():
            entries.append(
                {
                    "path": relative.as_posix(),
                    "type": "file",
                    "mode": metadata.st_mode & 0o777,
                    "size": metadata.st_size,
                    "sha256": file_sha256(path),
                }
            )
        elif path.is_dir():
            entries.append(
                {
                    "path": relative.as_posix(),
                    "type": "directory",
                    "mode": metadata.st_mode & 0o777,
                }
            )
    return entries


def _redact_environment(value: str, environment: dict[str, str]) -> str:
    markers = ("api_key", "authorization", "credential", "password", "secret", "token")
    secrets = {
        item
        for key, item in environment.items()
        if any(marker in key.casefold() for marker in markers) and len(item) >= 4
    }
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "<redacted>")
    return value
