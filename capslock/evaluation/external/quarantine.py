"""Versioned quarantine entries for reproducibly broken benchmark tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .contracts import GIT_SHA_RE, SHA256_RE, ExternalTask, canonical_hash
from .io import read_jsonl

ALLOWED_REASONS = {
    "gold_or_oracle_unstable",
    "image_damaged",
    "upstream_grader_defect",
}


@dataclass(frozen=True)
class QuarantineEntry:
    schema_version: int
    suite: str
    instance_id: str
    upstream_revision: str
    reason: str
    observed_failure: str
    reproduction_log_sha256: str
    recorded_at: str
    reviewer: str
    upstream_issue_url: str
    entry_hash: str

    def hash_payload(self) -> dict[str, object]:
        value = vars(self).copy()
        value.pop("entry_hash")
        return value

    def validate(self, *, suite: str, upstream_revision: str) -> None:
        if self.schema_version != 1 or self.suite != suite:
            raise ValueError(f"invalid quarantine identity for {self.instance_id}")
        if self.upstream_revision != upstream_revision or not GIT_SHA_RE.fullmatch(
            self.upstream_revision
        ):
            raise ValueError(f"stale quarantine revision for {self.instance_id}")
        if self.reason not in ALLOWED_REASONS:
            raise ValueError(f"disallowed quarantine reason for {self.instance_id}")
        if not SHA256_RE.fullmatch(self.reproduction_log_sha256):
            raise ValueError(f"invalid quarantine evidence hash for {self.instance_id}")
        if not all((self.observed_failure, self.recorded_at, self.reviewer)):
            raise ValueError(f"incomplete quarantine evidence for {self.instance_id}")
        issue = urlparse(self.upstream_issue_url)
        if issue.scheme != "https" or not issue.netloc:
            raise ValueError(f"invalid upstream issue URL for {self.instance_id}")
        if self.entry_hash != canonical_hash(self.hash_payload()):
            raise ValueError(f"quarantine entry hash mismatch for {self.instance_id}")


def load_quarantine(
    root: Path, *, suite: str, upstream_revision: str
) -> list[QuarantineEntry]:
    path = root / f"{suite}.jsonl"
    if not path.is_file():
        return []
    entries = [QuarantineEntry(**row) for row in read_jsonl(path)]
    for entry in entries:
        entry.validate(suite=suite, upstream_revision=upstream_revision)
    identifiers = [entry.instance_id for entry in entries]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"duplicate quarantine instance ID in {path}")
    return sorted(entries, key=lambda entry: entry.instance_id)


def apply_quarantine(
    tasks: list[ExternalTask], entries: list[QuarantineEntry]
) -> list[ExternalTask]:
    task_ids = {task.instance_id for task in tasks}
    missing = sorted({entry.instance_id for entry in entries} - task_ids)
    if missing:
        raise ValueError(f"quarantine contains unknown tasks: {', '.join(missing)}")
    excluded = {entry.instance_id for entry in entries}
    return [task for task in tasks if task.instance_id not in excluded]


def quarantine_hash(entries: list[QuarantineEntry]) -> str:
    return canonical_hash([vars(entry) for entry in entries])
