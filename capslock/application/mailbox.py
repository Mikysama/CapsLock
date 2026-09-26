"""Safe mailbox metadata shared by terminal and protocol clients."""

from __future__ import annotations

import math
from typing import Any


_COUNT_FIELDS = frozenset(
    {
        "pending_count",
        "pending_bytes",
        "query_count",
        "empty_read_count",
        "ack_retry_count",
        "deduplicated_count",
        "received_count",
        "delivered_count",
    }
)
_DURATION_FIELDS = frozenset(
    {"notification_latency_ms", "receipt_latency_ms", "delivery_latency_ms"}
)
_WAKE_REASONS = frozenset(
    {
        "task_completed",
        "message_available",
        "received",
        "delivered",
        "timeout",
        "cancelled",
        "closed",
    }
)


def mailbox_metadata(value: object) -> dict[str, Any]:
    """Allow only typed diagnostics; message envelopes and bodies stay private."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in _COUNT_FIELDS:
        number = value.get(key)
        if type(number) is int and number >= 0:
            result[key] = number
    for key in _DURATION_FIELDS:
        number = value.get(key)
        if type(number) in {float, int} and math.isfinite(number) and number >= 0:
            result[key] = number
    for key in ("receiver_active", "has_more"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    reason = value.get("wake_reason")
    if isinstance(reason, str) and reason in _WAKE_REASONS:
        result["wake_reason"] = reason
    return result


def mailbox_notice(value: object) -> str | None:
    metadata = mailbox_metadata(value)
    pending = metadata.get("pending_count", 0)
    if not pending:
        return None
    noun = "message" if pending == 1 else "messages"
    size = metadata.get("pending_bytes")
    suffix = f" ({size} bytes)" if size is not None else ""
    if metadata.get("receiver_active") is False:
        suffix += "; explicit follow-up required"
    return f"Agent mailbox: {pending} pending {noun}{suffix}"
