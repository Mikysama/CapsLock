"""Filesystem, locking, journal, and SQLite lifecycle primitives."""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from ..layout import ProjectLayout
from .archive import write_json
from .errors import LifecycleError


def sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    original = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        original.backup(copy)
    finally:
        original.close()
        copy.close()
    target.chmod(0o600)


class LifecycleIO:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout

    @contextmanager
    def locks(self) -> Iterator[None]:
        import fcntl

        paths = sorted(
            (
                self.layout.root / "state" / "lifecycle.lock",
                self.layout.user.home / "state" / "lifecycle.lock",
            )
        )
        with ExitStack() as stack:
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = stack.enter_context(path.open("a+"))
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def journal(self, operation: str, source: str, recovery: str) -> Path:
        path = self.layout.root / "state" / "lifecycle-journal.json"
        write_json(
            path,
            {
                "operation": operation,
                "source": source,
                "recovery": recovery,
                "started_at": datetime.now(UTC).isoformat(),
            },
        )
        path.chmod(0o600)
        return path

    @staticmethod
    def snapshot_database(source: Path, target: Path, missing: list[str]) -> None:
        if source.is_file():
            sqlite_backup(source, target)
        else:
            missing.append(str(source))

    @staticmethod
    def copy_tree(source: Path, target: Path) -> None:
        if not source.is_dir():
            return
        symlink = next((path for path in source.rglob("*") if path.is_symlink()), None)
        if symlink is not None:
            raise LifecycleError(f"backup does not follow symbolic links: {symlink}")
        shutil.copytree(
            source,
            target,
            symlinks=False,
            ignore_dangling_symlinks=True,
        )

    @staticmethod
    def replace_tree(source: Path, target: Path) -> None:
        temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, temporary)
        if target.exists():
            shutil.rmtree(target)
        os.replace(temporary, target)

    @staticmethod
    def atomic_replace(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
