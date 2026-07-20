"""Memory content, confidence, expiry, and transfer path validation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..policy import PolicyError, WorkspacePolicy
from ..security import sanitize_memory_text

MAX_MEMORY_BYTES = 8 * 1024
MAX_TRANSFER_BYTES = 5 * 1024 * 1024
MAX_TRANSFER_RECORDS = 1000


def validated_text(value: Any) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("memory content must be a non-empty string")
    safe, rules = sanitize_memory_text(value.strip())
    if len(safe.encode("utf-8")) > MAX_MEMORY_BYTES:
        raise ValueError(f"memory content exceeds the {MAX_MEMORY_BYTES} byte limit")
    return safe, rules


def confidence(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("memory confidence must be a number from 0 to 1") from exc
    if not 0 <= result <= 1:
        raise ValueError("memory confidence must be a number from 0 to 1")
    return result


def expiry(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("memory expiry must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("memory expiry must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("memory expiry must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def transfer_path(workspace: Path, requested: str, *, writing: bool) -> Path:
    if not requested:
        raise ValueError("provide a workspace-relative JSON path")
    relative = Path(requested)
    if relative.is_absolute() or ".." in relative.parts:
        raise PolicyError("memory transfer requires a workspace-relative path")
    current = workspace
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise PolicyError("memory transfer does not follow symbolic links")
    policy = WorkspacePolicy(workspace, max_file_bytes=MAX_TRANSFER_BYTES)
    path = policy.resolve(requested)
    if path.suffix.casefold() != ".json":
        raise PolicyError("memory transfer path must end in .json")
    if writing:
        policy.writable_file(requested, create=not path.exists())
    else:
        policy.readable_file(requested)
    return path
