"""Read-only workspace policy shared by every local tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .security import TEXT_SUFFIXES


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

    def writable_file(self, requested_path: str, *, create: bool = False) -> Path:
        """Validate a controlled text-file write without performing it."""
        path = self.resolve(requested_path)
        relative = path.relative_to(self.root)
        if any(part in {".git", ".capslock"} for part in relative.parts):
            raise PolicyError("writes to .git and .capslock are not allowed")
        if path.exists():
            if path.is_dir():
                raise PolicyError(f"path is a directory: {path}")
            if path.suffix.lower() not in TEXT_SUFFIXES:
                raise PolicyError(f"unsupported text file type: {path.suffix or '(none)'}")
            self.readable_file(requested_path)
        elif not create:
            raise PolicyError(f"file does not exist: {path}")
        else:
            if not path.parent.is_dir():
                raise PolicyError(f"parent directory does not exist: {path.parent}")
            if path.suffix.lower() not in TEXT_SUFFIXES:
                raise PolicyError(f"unsupported text file type: {path.suffix or '(none)'}")
        return path

    def validate_write_content(self, content: str) -> None:
        if len(content.encode("utf-8")) > self.max_file_bytes:
            raise PolicyError(f"file exceeds the {self.max_file_bytes} byte write limit")
