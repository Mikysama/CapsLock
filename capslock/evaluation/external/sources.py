"""Pinned upstream source and dataset lock management."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .contracts import (
    GIT_SHA_RE,
    SHA256_RE,
    ExternalTask,
    SuiteDefinition,
    canonical_hash,
)
from .io import read_json, write_json


def sync_harness(definition: SuiteDefinition, cache_root: Path) -> Path:
    destination = cache_root / "harnesses" / definition.id
    git = destination / ".git"
    if not git.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(destination)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(destination),
                "remote",
                "add",
                "origin",
                definition.upstream_repo,
            ],
            check=True,
        )
    subprocess.run(
        [
            "git",
            "-C",
            str(destination),
            "fetch",
            "--depth",
            "1",
            "origin",
            definition.upstream_revision,
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(destination), "checkout", "--detach", "FETCH_HEAD"],
        check=True,
    )
    actual = subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual != definition.upstream_revision:
        raise RuntimeError(f"upstream revision mismatch for {definition.id}")
    return destination


def resolve_dataset_revision(dataset_id: str, requested: str) -> str:
    if requested != "resolve-at-sync" and len(requested) == 40:
        return requested
    url = f"https://huggingface.co/datasets/{dataset_id}"
    completed = subprocess.run(
        ["git", "ls-remote", url, "refs/heads/main"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode or not completed.stdout.strip():
        raise RuntimeError(
            f"could not resolve immutable dataset revision for {dataset_id}: "
            f"{completed.stderr.strip()}"
        )
    revision = completed.stdout.split()[0]
    if len(revision) != 40:
        raise RuntimeError(f"invalid dataset revision returned for {dataset_id}")
    return revision


def write_source_lock(
    definition: SuiteDefinition,
    cache_root: Path,
    *,
    tasks: list[ExternalTask] | None = None,
    allow_existing: bool = True,
) -> Path:
    path = cache_root / "locks" / f"{definition.id}.json"
    revisions = {
        dataset_id: resolve_dataset_revision(dataset_id, definition.dataset_revision)
        for dataset_id in definition.dataset_ids
    }
    payload = {
        "schema_version": 1,
        "suite": definition.id,
        "upstream_repo": definition.upstream_repo,
        "upstream_revision": definition.upstream_revision,
        "dataset_revisions": revisions,
        "adapter": definition.adapter,
        "artifact_kind": definition.artifact_kind.value,
        "license": definition.license,
        "core_split": definition.core_split,
        "full_split": definition.full_split,
        "excluded_splits": list(definition.excluded_splits),
        "resource_classes": list(definition.resource_classes),
        "created_at": datetime.now(UTC).isoformat(),
    }
    if tasks is not None:
        ordered = sorted(tasks, key=lambda task: task.instance_id)
        payload.update(
            {
                "task_count": len(ordered),
                "task_ids_sha256": canonical_hash(
                    [task.instance_id for task in ordered]
                ),
                "task_manifest_sha256": canonical_hash(
                    [task.manifest_payload() for task in ordered]
                ),
                "grader_config_sha256": canonical_hash(
                    {
                        task.instance_id: task.manifest_payload()["grader"]
                        for task in ordered
                    }
                ),
            }
        )
    payload["lock_hash"] = canonical_hash(payload)
    if path.exists() and allow_existing:
        existing = read_json(path)
        comparable = dict(existing)
        comparable.pop("created_at", None)
        comparable.pop("lock_hash", None)
        proposed = dict(payload)
        proposed.pop("created_at", None)
        proposed.pop("lock_hash", None)
        if comparable != proposed:
            raise RuntimeError(
                f"source lock changed for {definition.id}; use a fresh cache or version"
            )
        return path
    write_json(path, payload)
    return path


def validate_source_lock(value: dict[str, object], *, suite: str) -> str:
    required = {
        "schema_version",
        "suite",
        "upstream_repo",
        "upstream_revision",
        "dataset_revisions",
        "adapter",
        "artifact_kind",
        "license",
        "core_split",
        "full_split",
        "excluded_splits",
        "resource_classes",
        "created_at",
        "lock_hash",
    }
    task_fields = {
        "task_count",
        "task_ids_sha256",
        "task_manifest_sha256",
        "grader_config_sha256",
    }
    unknown = set(value) - required - task_fields
    missing = required - set(value)
    if unknown or missing:
        raise ValueError(
            f"invalid source lock fields for {suite}; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    if value.get("schema_version") != 1 or value.get("suite") != suite:
        raise ValueError(f"invalid source lock identity for {suite}")
    if not GIT_SHA_RE.fullmatch(str(value.get("upstream_revision", ""))):
        raise ValueError(f"invalid upstream revision in source lock for {suite}")
    revisions = value.get("dataset_revisions")
    if not isinstance(revisions, dict) or any(
        not GIT_SHA_RE.fullmatch(str(revision)) for revision in revisions.values()
    ):
        raise ValueError(f"invalid dataset revisions in source lock for {suite}")
    present_task_fields = set(value) & task_fields
    if present_task_fields and present_task_fields != task_fields:
        raise ValueError(f"incomplete task hashes in source lock for {suite}")
    if present_task_fields:
        if not isinstance(value["task_count"], int) or value["task_count"] < 1:
            raise ValueError(f"invalid task count in source lock for {suite}")
        for key in task_fields - {"task_count"}:
            if not SHA256_RE.fullmatch(str(value[key])):
                raise ValueError(f"invalid {key} in source lock for {suite}")
    lock_hash = str(value.get("lock_hash", ""))
    payload = dict(value)
    payload.pop("lock_hash", None)
    if lock_hash != canonical_hash(payload):
        raise ValueError(f"source lock hash mismatch for {suite}")
    return lock_hash


def validate_catalog_lock(
    value: dict[str, object], tasks: list[ExternalTask], *, suite: str
) -> None:
    validate_source_lock(value, suite=suite)
    ordered = sorted(tasks, key=lambda task: task.instance_id)
    expected = {
        "task_count": len(ordered),
        "task_ids_sha256": canonical_hash([task.instance_id for task in ordered]),
        "task_manifest_sha256": canonical_hash(
            [task.manifest_payload() for task in ordered]
        ),
        "grader_config_sha256": canonical_hash(
            {task.instance_id: task.manifest_payload()["grader"] for task in ordered}
        ),
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"source lock {key} does not match catalog for {suite}")


def validate_public_repository_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "github.com",
        "huggingface.co",
    }:
        raise ValueError(
            f"external source must be a public GitHub/Hugging Face URL: {url}"
        )
