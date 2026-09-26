"""Canonical public run-event envelope shared by local transports."""

from ..domain import AgentEvent

EVENT_SCHEMA_VERSION = 3


def event_record(event: AgentEvent) -> dict[str, object]:
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "sequence": event.sequence,
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "timestamp": event.timestamp,
        "session_id": event.session_id,
        "work_item_id": event.work_item_id,
        "run_id": event.run_id,
        "event": event.kind.value,
        "status": str(event.data.get("status", "running")),
        "terminal": event.terminal,
        "data": event.data,
    }
