"""Read-only workspace policy shared by every local tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PolicyError(ValueError):
    """Raised when an operation falls outside the read-only workspace policy."""


@dataclass(frozen=True)
class WorkspacePolicy:
    root: Path
    max_file_bytes: int = 512_000
    max_files: int = 1_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def resolve(self, requested_path: str = ".") -> Path:
        candidate = Path(requested_path).expanduser()
        path = (self.root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if not path.is_relative_to(self.root):
            raise PolicyError(f"path must be inside the allowed workspace: {self.root}")
        return path

    def readable_file(self, requested_path: str) -> Path:
        path = self.resolve(requested_path)
        if not path.is_file():
            raise PolicyError(f"file does not exist: {path}")
        if path.stat().st_size > self.max_file_bytes:
            raise PolicyError(f"file exceeds the {self.max_file_bytes} byte read limit: {path}")
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyError(f"binary or non-UTF-8 files are not supported: {path}") from exc
        return path
