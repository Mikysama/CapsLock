"""Small, secret-safe runtime event sink."""

from __future__ import annotations

import json
import time
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
        self._buffer: list[str] = []
        self._buffer_bytes = 0
        self._last_flush = time.monotonic()

    def emit(self, event_kind: str, **data: Any) -> None:
        event = Event(event_kind, data)
        self.events.append(event)
        if self.path:
            safe = json.dumps(redact(asdict(event)), ensure_ascii=False)
            self._buffer.append(safe)
            self._buffer_bytes += len(safe.encode("utf-8")) + 1
            terminal = event_kind == "workflow_event" and data.get("event") in {
                "completed",
                "failed",
                "cancelled",
                "stopped",
                "waiting_approval",
            }
            if (
                terminal
                or self._buffer_bytes >= 4096
                or time.monotonic() - self._last_flush >= 0.050
            ):
                self.flush()

    def flush(self) -> None:
        if not self.path or not self._buffer:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        records, self._buffer = self._buffer, []
        self._buffer_bytes = 0
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(records) + "\n")
        self._last_flush = time.monotonic()

    def mark(self) -> int:
        return len(self.events)

    def since(self, mark: int) -> list[Event]:
        return list(self.events[mark:])
