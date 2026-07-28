"""Portable coordination for CapsLock-managed workspace mutations."""

from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_REGISTRY_GUARD = threading.Lock()
_WORKSPACE_LOCKS: dict[Path, threading.RLock] = {}


class WorkspaceMutationCoordinator:
    """Serialize workspace writes in-process and, when configured, cross-process."""

    def __init__(self, lock_root: Path | None = None) -> None:
        self.lock_root = lock_root.resolve() if lock_root is not None else None

    @contextmanager
    def lock(self, workspace: Path) -> Iterator[None]:
        root = workspace.resolve()
        with _REGISTRY_GUARD:
            local = _WORKSPACE_LOCKS.setdefault(root, threading.RLock())
        with local:
            handle = self._open_lock(root)
            try:
                if handle is not None:
                    _lock_file(handle)
                yield
            finally:
                if handle is not None:
                    try:
                        _unlock_file(handle)
                    finally:
                        handle.close()

    def _open_lock(self, workspace: Path):
        if self.lock_root is None:
            return None
        self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()
        path = self.lock_root / f"{digest}.lock"
        handle = path.open("a+b")
        path.chmod(0o600)
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        return handle


def _lock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["WorkspaceMutationCoordinator"]
