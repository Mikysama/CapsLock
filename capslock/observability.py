"""Small, secret-safe runtime event sink."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .security import redact


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict[str, Any]


class EventSink:
    def __init__(self, path: Path | None = None) -> None:
        self.events: list[Event] = []
        self.path = path

    def emit(self, event_kind: str, **data: Any) -> None:
        event = Event(event_kind, data)
        self.events.append(event)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            safe = json.dumps(redact(asdict(event)), ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(safe + "\n")

    def mark(self) -> int:
        return len(self.events)

    def since(self, mark: int) -> list[Event]:
        return list(self.events[mark:])
