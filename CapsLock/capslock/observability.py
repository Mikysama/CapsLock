"""Small, secret-safe runtime event sink."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|token)\s*[=:]\s*[^\s,]+")


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict[str, Any]


class EventSink:
    def __init__(self, path: Path | None = None) -> None:
        self.events: list[Event] = []
        self.path = path

    def emit(self, kind: str, **data: Any) -> None:
        event = Event(kind, data)
        self.events.append(event)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            safe = _SECRET.sub("\\1=<redacted>", json.dumps(asdict(event), ensure_ascii=False))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(safe + "\n")
