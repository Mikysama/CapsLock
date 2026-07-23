"""Workspace backup creation, listing, and restoration."""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ..layout import ProjectLayout
from ..storage.memory_repositories import workspace_key
from .archive import (
    build_manifest,
    extract_zip,
    read_json,
    verify_archive,
    write_json,
    write_zip,
)
from .errors import LifecycleError
from .io import LifecycleIO
from .sanitization import sanitize_config, sanitize_mcp


BACKUP_FORMAT = "capslock-backup"
ARCHIVE_VERSION = 3
SUPPORTED_ARCHIVE_VERSIONS = frozenset({ARCHIVE_VERSION})


class BackupService:
    def __init__(
        self,
        layout: ProjectLayout,
        memory_path: Path,
        io: LifecycleIO,
    ) -> None:
        self.layout = layout
        self.memory_path = memory_path
        self.io = io

    @property
    def directory(self) -> Path:
        identity = workspace_key(self.layout.workspace)[:16]
        return self.layout.user.home / "backups" / identity

    def create(self, destination: Path | None = None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = destination or self.directory / f"capslock-{stamp}.clbackup"
        target = target.expanduser().resolve()
        if target.exists():
            raise FileExistsError(f"backup already exists: {target}")
        with (
            self.io.locks(),
            tempfile.TemporaryDirectory(prefix="capslock-backup-") as raw,
        ):
            stage = Path(raw)
            missing: list[str] = []
            self.io.snapshot_database(
                self.layout.database,
                stage / "workspace.sqlite3",
                missing,
            )
            self.io.snapshot_database(
                self.memory_path,
                stage / "memory.sqlite3",
                missing,
            )
            for source, name in (
                (self.layout.config, "config.toml"),
                (self.layout.project_mcp, "mcp.json"),
                (self.layout.local_mcp, "local-mcp.json"),
                (self.layout.events, "events.jsonl"),
            ):
                if not source.is_file():
                    continue
                if name == "config.toml":
                    if sanitize_config(source, stage / name):
                        missing.append("plaintext configuration credentials")
                elif name == "local-mcp.json":
                    write_json(stage / name, sanitize_mcp(read_json(source)))
                    missing.append("MCP environment values")
                else:
                    shutil.copy2(source, stage / name)
            self.io.copy_tree(self.layout.skills, stage / "project-skills")
            self.io.copy_tree(self.layout.user.skills, stage / "user-skills")
            self.io.copy_tree(self.layout.artifacts, stage / "artifacts")
            manifest = build_manifest(
                BACKUP_FORMAT,
                stage,
                workspace=self.layout.workspace,
                version=ARCHIVE_VERSION,
                extra={"missing_secrets": sorted(set(missing))},
            )
            write_json(stage / "manifest.json", manifest)
            write_zip(stage, target)
        target.chmod(0o600)
        return target

    def list(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.glob("*.clbackup"), reverse=True)

    def restore(self, archive: Path) -> Path:
        verify_archive(
            archive,
            supported_versions=SUPPORTED_ARCHIVE_VERSIONS,
            expected_format=BACKUP_FORMAT,
        )
        safety = self.create()
        journal = self.io.journal("restore", str(archive), str(safety))
        try:
            with (
                self.io.locks(),
                tempfile.TemporaryDirectory(prefix="capslock-restore-") as raw,
            ):
                stage = Path(raw)
                extract_zip(archive, stage)
                replacements = (
                    (stage / "workspace.sqlite3", self.layout.database),
                    (stage / "memory.sqlite3", self.memory_path),
                    (stage / "config.toml", self.layout.config),
                    (stage / "mcp.json", self.layout.project_mcp),
                    (stage / "local-mcp.json", self.layout.local_mcp),
                    (stage / "events.jsonl", self.layout.events),
                )
                for source, destination in replacements:
                    if source.exists():
                        self.io.atomic_replace(source, destination)
                    elif destination.exists():
                        destination.unlink()
                for source, destination in (
                    (stage / "project-skills", self.layout.skills),
                    (stage / "user-skills", self.layout.user.skills),
                    (stage / "artifacts", self.layout.artifacts),
                ):
                    if source.is_dir():
                        self.io.replace_tree(source, destination)
                    elif destination.exists():
                        shutil.rmtree(destination)
            journal.unlink(missing_ok=True)
            return safety
        except Exception:
            raise LifecycleError(
                f"restore failed; recovery backup: {safety}; journal: {journal}"
            )
