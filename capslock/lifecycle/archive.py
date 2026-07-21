"""Bounded, path-safe lifecycle archive I/O."""

from __future__ import annotations

import json
import os
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import LifecycleError

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_000


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"JSON document must be an object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def write_zip(stage: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as bundle:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(stage).as_posix())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def validate_zip(bundle: zipfile.ZipFile) -> None:
    items = bundle.infolist()
    if len(items) > MAX_ARCHIVE_FILES:
        raise LifecycleError("archive contains too many files")
    if len({item.filename for item in items}) != len(items):
        raise LifecycleError("archive contains duplicate member names")
    total = 0
    for item in items:
        path = PurePosixPath(item.filename)
        mode = item.external_attr >> 16
        if (
            path.is_absolute()
            or ".." in path.parts
            or stat.S_ISLNK(mode)
            or stat.S_ISCHR(mode)
            or stat.S_ISBLK(mode)
        ):
            raise LifecycleError(f"unsafe archive member: {item.filename}")
        total += item.file_size
        if total > MAX_ARCHIVE_BYTES:
            raise LifecycleError("expanded archive exceeds the size limit")


def extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        validate_zip(bundle)
        for item in bundle.infolist():
            destination = target / PurePosixPath(item.filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not item.is_dir():
                destination.write_bytes(bundle.read(item))
