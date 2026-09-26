"""Process leases protecting crash recovery from a second live workspace owner."""

from __future__ import annotations

import fcntl
import os
import threading
from dataclasses import dataclass
from pathlib import Path


class WorkspaceBusyError(RuntimeError):
    pass


@dataclass
class WorkspaceLease:
    path: Path
    descriptor: int
    references: int = 1
    ready: bool = False


_leases: dict[tuple[int, Path], WorkspaceLease] = {}
_guard = threading.Lock()


def acquire_workspace_lease(
    path: Path, *, shared_owner: bool
) -> tuple[WorkspaceLease, bool]:
    canonical = path.resolve()
    key = (os.getpid(), canonical)
    with _guard:
        existing = _leases.get(key)
        if existing is not None:
            if not shared_owner or not existing.ready:
                raise WorkspaceBusyError(
                    f"workspace database is already open: {canonical}"
                )
            existing.references += 1
            return existing, False
        canonical.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            str(canonical) + ".owner.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise WorkspaceBusyError(
                f"workspace database is already open: {canonical}"
            ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        lease = WorkspaceLease(canonical, descriptor)
        _leases[key] = lease
        return lease, True


def release_workspace_lease(lease: WorkspaceLease) -> None:
    with _guard:
        key = (os.getpid(), lease.path)
        if _leases.get(key) is not lease:
            return
        lease.references -= 1
        if lease.references == 0:
            del _leases[key]
            os.close(lease.descriptor)
