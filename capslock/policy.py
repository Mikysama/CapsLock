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
        unresolved = self.root / candidate if not candidate.is_absolute() else candidate
        self._reject_symlink_components(unresolved)
        path = unresolved.resolve()
        if not path.is_relative_to(self.root):
            raise PolicyError(f"path must be inside the allowed workspace: {self.root}")
        return path

    def command_directory(self, requested_path: str = ".") -> Path:
        path = self.resolve(requested_path)
        relative = path.relative_to(self.root)
        if not path.is_dir() or any(
            part in {".git", ".capslock"} for part in relative.parts
        ):
            raise PolicyError("command cwd must be a normal workspace directory")
        return path

    def readable_file(self, requested_path: str) -> Path:
        path = self.resolve(requested_path)
        self._ensure_agent_readable(path)
        if not path.is_file():
            raise PolicyError(f"file does not exist: {path}")
        if path.stat().st_size > self.max_file_bytes:
            raise PolicyError(
                f"file exceeds the {self.max_file_bytes} byte read limit: {path}"
            )
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyError(
                f"binary or non-UTF-8 files are not supported: {path}"
            ) from exc
        return path

    def readable_binary_file(
        self, requested_path: str, *, max_bytes: int | None = None
    ) -> Path:
        """Validate a bounded non-text file without attempting UTF-8 decoding."""
        path = self.resolve(requested_path)
        self._ensure_agent_readable(path)
        if not path.is_file():
            raise PolicyError(f"file does not exist: {path}")
        limit = self.max_file_bytes if max_bytes is None else max_bytes
        if path.stat().st_size > limit:
            raise PolicyError(f"file exceeds the {limit} byte read limit: {path}")
        return path

    def writable_file(self, requested_path: str, *, create: bool = False) -> Path:
        """Validate a controlled text-file write without performing it."""
        path = self.resolve(requested_path)
        relative = path.relative_to(self.root)
        if ".git" in relative.parts:
            raise PolicyError("writes to .git are not allowed")
        if ".capslock" in relative.parts and not self.is_skill_path(path):
            raise PolicyError(
                "writes within .capslock are only allowed for project Skills"
            )
        if path.exists():
            if path.is_dir():
                raise PolicyError(f"path is a directory: {path}")
            if path.suffix.lower() not in TEXT_SUFFIXES:
                raise PolicyError(
                    f"unsupported text file type: {path.suffix or '(none)'}"
                )
            self.readable_file(requested_path)
        elif not create:
            raise PolicyError(f"file does not exist: {path}")
        else:
            if not path.parent.is_dir():
                raise PolicyError(f"parent directory does not exist: {path.parent}")
            if path.suffix.lower() not in TEXT_SUFFIXES:
                raise PolicyError(
                    f"unsupported text file type: {path.suffix or '(none)'}"
                )
        return path

    def readable_directory(self, requested_path: str = ".") -> Path:
        path = self.resolve(requested_path)
        self._ensure_agent_readable(path, directory=True)
        if not path.is_dir():
            raise PolicyError(f"directory does not exist: {path}")
        return path

    def is_agent_readable(self, path: Path) -> bool:
        try:
            self._ensure_agent_readable(path, directory=path.is_dir())
        except PolicyError:
            return False
        return True

    def is_skill_path(self, path: Path) -> bool:
        relative = path.relative_to(self.root)
        return len(relative.parts) >= 3 and relative.parts[:2] == (
            ".capslock",
            "skills",
        )

    def _ensure_agent_readable(self, path: Path, *, directory: bool = False) -> None:
        relative = path.relative_to(self.root)
        if relative.name == ".env" or (
            relative.name.startswith(".env.") and relative.name != ".env.example"
        ):
            raise PolicyError("project environment files are private")
        if not relative.parts or relative.parts[0] != ".capslock":
            return
        if directory and relative.parts == (".capslock",):
            return
        allowed_file = relative.parts in {
            (".capslock", "config.toml"),
            (".capslock", "mcp.json"),
        }
        allowed_skill = len(relative.parts) >= 2 and relative.parts[:2] == (
            ".capslock",
            "skills",
        )
        if not allowed_file and not allowed_skill:
            raise PolicyError("CapsLock local and runtime state is private")

    def _reject_symlink_components(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise PolicyError(
                    f"workspace paths must not contain symlinks: {current}"
                )

    def validate_write_content(self, content: str) -> None:
        if len(content.encode("utf-8")) > self.max_file_bytes:
            raise PolicyError(
                f"file exceeds the {self.max_file_bytes} byte write limit"
            )
