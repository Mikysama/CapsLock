"""Stable contracts for external benchmark manifests and results."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "auth_token",
    "authorization",
    "authorization_header",
    "bearer_token",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
}
HIDDEN_TASK_KEYS = {
    "fail_to_pass",
    "fix_patch",
    "gold_patch",
    "hint",
    "hints",
    "pass_to_pass",
    "test_patch",
}


class ArtifactKind(StrEnum):
    GIT_PATCH = "git_patch"
    ENVIRONMENT_STATE = "environment_state"


class TaskTerminalStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class GraderStatus(StrEnum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def redact_secrets(value: Any) -> Any:
    """Return a canonicalisable copy with credential-like values removed."""
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>" if _is_sensitive_key(str(key)) else redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized in SENSITIVE_KEYS or any(
        normalized.endswith(f"_{marker}") for marker in SENSITIVE_KEYS
    )


def reject_hidden_task_data(value: object, *, location: str = "task") -> None:
    """Ensure agent-visible task data cannot contain official hidden fields."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in HIDDEN_TASK_KEYS:
                raise ValueError(f"hidden evaluation field at {location}.{key}")
            reject_hidden_task_data(item, location=f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            reject_hidden_task_data(item, location=f"{location}[{index}]")


@dataclass(frozen=True)
class ModelTrack:
    name: str
    provider: str
    model: str
    base_url: str
    credential_env: str
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    context_window: int = 128_000
    max_output_tokens: int = 8_192

    def __post_init__(self) -> None:
        if not all((self.name, self.provider, self.model, self.base_url)):
            raise ValueError("model track fields must be non-empty")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.credential_env):
            raise ValueError("credential_env must be an uppercase environment name")
        if min(self.input_cost_per_million, self.output_cost_per_million) < 0:
            raise ValueError("model pricing cannot be negative")
        if self.context_window <= 0 or self.max_output_tokens <= 0:
            raise ValueError("model token limits must be positive")
        if self.max_output_tokens > self.context_window:
            raise ValueError("model output limit cannot exceed context window")


@dataclass(frozen=True)
class SuiteDefinition:
    id: str
    adapter: str
    artifact_kind: ArtifactKind
    upstream_repo: str
    upstream_revision: str
    license: str
    core_size: int
    full_size: int | None
    harness_probe: tuple[str, ...]
    dataset_ids: tuple[str, ...] = ()
    dataset_revision: str = "resolve-at-sync"
    core_split: str = ""
    full_split: str = ""
    excluded_splits: tuple[str, ...] = ()
    resource_classes: tuple[str, ...] = ("linux-amd64-cpu",)

    def __post_init__(self) -> None:
        if not self.id or not self.adapter or not self.upstream_repo:
            raise ValueError("suite id, adapter, and upstream_repo are required")
        if not GIT_SHA_RE.fullmatch(self.upstream_revision):
            raise ValueError(f"suite {self.id} has an invalid upstream revision")
        if self.core_size <= 0 or (self.full_size is not None and self.full_size <= 0):
            raise ValueError("suite sizes must be positive")
        if not self.harness_probe:
            raise ValueError("harness_probe must not be empty")
        if "windows" in {item.casefold() for item in self.excluded_splits}:
            return


@dataclass(frozen=True)
class ExternalTask:
    suite: str
    instance_id: str
    problem_statement: str
    workspace_source: str
    repository: str = ""
    language: str = "unknown"
    task_type: str = "unknown"
    gold_patch_size: int = 0
    resource_class: str = "linux-amd64-cpu"
    grader: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.suite or not self.instance_id or not self.problem_statement.strip():
            raise ValueError(
                "task suite, instance_id, and problem_statement are required"
            )
        if not self.workspace_source:
            raise ValueError("workspace_source is required")
        if self.gold_patch_size < 0:
            raise ValueError("gold_patch_size cannot be negative")
        reject_hidden_task_data(self.public_payload())

    def public_payload(self) -> dict[str, Any]:
        """The only task metadata that may enter the agent environment."""
        return {
            "suite": self.suite,
            "instance_id": self.instance_id,
            "problem_statement": self.problem_statement,
            "repository": self.repository,
            "language": self.language,
            "task_type": self.task_type,
            "resource_class": self.resource_class,
        }

    def manifest_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["grader"] = redact_secrets(self.grader)
        return payload


@dataclass(frozen=True)
class RunManifest:
    schema_version: int
    run_id: str
    suite: str
    suite_revision: str
    profile: str
    task_ids_sha256: str
    capslock_commit: str
    capslock_wheel_sha256: str
    model_track: str
    provider: str
    model: str
    prompt_sha256: str
    permission_profile: str
    limits: dict[str, int | float | None]
    repetition_count: int
    started_at: str
    environment: dict[str, str]
    pricing: dict[str, float]
    quarantine_sha256: str
    quarantine_count: int
    manifest_hash: str = ""

    def payload(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = redact_secrets(asdict(self))
        if not include_hash:
            value.pop("manifest_hash", None)
        return value

    def with_hash(self) -> "RunManifest":
        values = asdict(self)
        values["manifest_hash"] = canonical_hash(self.payload(include_hash=False))
        return type(self)(**values)

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("run manifest schema_version must be 1")
        for label, value in (
            ("suite_revision", self.suite_revision),
            ("task_ids_sha256", self.task_ids_sha256),
            ("capslock_wheel_sha256", self.capslock_wheel_sha256),
            ("prompt_sha256", self.prompt_sha256),
            ("quarantine_sha256", self.quarantine_sha256),
            ("manifest_hash", self.manifest_hash),
        ):
            if not SHA256_RE.fullmatch(value):
                raise ValueError(f"{label} is invalid")
        if self.quarantine_count < 0:
            raise ValueError("quarantine_count cannot be negative")
        if not all(
            (
                self.run_id,
                self.suite,
                self.profile,
                self.capslock_commit,
                self.model_track,
                self.provider,
                self.model,
                self.permission_profile,
                self.started_at,
            )
        ):
            raise ValueError("run manifest identity fields must be non-empty")
        expected = self.with_hash().manifest_hash
        if self.manifest_hash != expected:
            raise ValueError("run manifest hash mismatch")


@dataclass(frozen=True)
class TaskResult:
    schema_version: int
    suite: str
    suite_revision: str
    instance_id: str
    run_id: str
    run_ordinal: int
    model_track: str
    agent_terminal_status: str
    grader_status: str
    resolved: bool
    failure_category: str | None
    stop_reason: str | None
    artifact_kind: str
    artifact_sha256: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_seconds: float
    tool_rounds: int
    tool_calls: int
    changed_files: int
    changed_lines: int
    trajectory_path: str
    grader_log_path: str
    infrastructure_error: str | None = None
    result_hash: str = ""
    stderr_path: str = ""
    peak_context_tokens: int = 0
    context_updates: int = 0
    context_compactions: int = 0
    human_interventions: int | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TaskResult":
        """Verify stored v1 bytes semantically before adding optional defaults.

        Early v1 results omitted context diagnostics. Validate their original
        field set, then return a normalized, freshly hashed in-memory result.
        Reading never rewrites the source artifact or accepts an invalid hash.
        """
        result = cls(**payload)
        result.validate()
        expected = canonical_hash(
            {key: value for key, value in payload.items() if key != "result_hash"}
        )
        if result.result_hash != expected:
            raise ValueError(f"task result hash mismatch: {result.instance_id}")
        return result.with_hash()

    def payload(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = redact_secrets(asdict(self))
        if not include_hash:
            value.pop("result_hash", None)
        return value

    def with_hash(self) -> "TaskResult":
        values = asdict(self)
        values["result_hash"] = canonical_hash(self.payload(include_hash=False))
        return type(self)(**values)

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("task result schema_version must be 1")
        if self.run_ordinal < 1:
            raise ValueError("run_ordinal must be positive")
        try:
            GraderStatus(self.grader_status)
        except ValueError as exc:
            raise ValueError("grader_status is invalid") from exc
        try:
            ArtifactKind(self.artifact_kind)
        except ValueError as exc:
            raise ValueError("artifact_kind is invalid") from exc
        if (
            min(
                self.input_tokens,
                self.output_tokens,
                self.tool_rounds,
                self.tool_calls,
                self.changed_files,
                self.changed_lines,
                self.peak_context_tokens,
                self.context_updates,
                self.context_compactions,
            )
            < 0
        ):
            raise ValueError("usage values cannot be negative")
        if self.cost_usd < 0 or self.duration_seconds < 0:
            raise ValueError("cost and duration cannot be negative")
        if self.human_interventions is not None and self.human_interventions < 0:
            raise ValueError("human interventions cannot be negative")
        if self.resolved and self.grader_status != GraderStatus.PASSED:
            raise ValueError("resolved results require a passed grader")
        if self.resolved and (
            self.agent_terminal_status != TaskTerminalStatus.COMPLETED
            or self.stop_reason is not None
            or self.infrastructure_error is not None
        ):
            raise ValueError("resolved results require a clean completed rollout")
        if self.artifact_sha256 is not None and not SHA256_RE.fullmatch(
            self.artifact_sha256
        ):
            raise ValueError("artifact_sha256 is invalid")
        if not SHA256_RE.fullmatch(self.result_hash):
            raise ValueError("result_hash is invalid")
