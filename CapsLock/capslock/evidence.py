"""Line-addressable evidence returned by read-only workspace tools."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class Evidence:
    """A verifiable excerpt of one file in the active workspace."""

    path: Path
    start_line: int
    end_line: int
    text: str

    @property
    def id(self) -> str:
        value = f"{self.path.resolve()}:{self.start_line}:{self.end_line}".encode()
        return "ev_" + sha256(value).hexdigest()[:16]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "path": str(self.path),
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
        }
