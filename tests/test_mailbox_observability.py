"""Mailbox diagnostics remain metadata-only across local clients."""

from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

from capslock.application.events import event_record
from capslock.cli.fullscreen_tui.models import (
    ContextViewModel,
    TuiState,
    reduce_event,
)
from capslock.cli.tui import _RunRenderer, _TerminalWriter
from capslock.domain import AgentEvent, AgentEventKind
from capslock.theme import make_console
from capslock.application.mailbox import mailbox_metadata


def _mailbox_event() -> AgentEvent:
    return AgentEvent(
        1,
        "now",
        "session",
        "run",
        "work",
        AgentEventKind.CONTEXT_UPDATED,
        {
            "mailbox": {
                "pending_count": 3,
                "pending_bytes": 240,
                "receiver_active": False,
                "wake_reason": "message_available",
                "payload": "private agent message",
            }
        },
    )


def test_mailbox_event_envelope_excludes_message_bodies() -> None:
    event = _mailbox_event()
    record = event_record(event)
    assert record["schema_version"] == 3
    assert record["data"]["mailbox"] == {
        "pending_count": 3,
        "pending_bytes": 240,
        "receiver_active": False,
        "wake_reason": "message_available",
    }
    assert event.data["mailbox"]["payload"] == "private agent message"


def test_fullscreen_mailbox_notice_preserves_context_and_paused_state() -> None:
    original = TuiState(
        context=ContextViewModel(100, 1000), activity="Waiting for approval"
    )
    state = reduce_event(original, _mailbox_event())
    assert state.context == original.context
    assert state.activity == original.activity
    assert state.notification == (
        "Agent mailbox: 3 pending messages (240 bytes); explicit follow-up required"
    )
    assert not state.messages


def test_late_mailbox_notice_does_not_reactivate_a_finished_run() -> None:
    original = TuiState(
        terminal_runs=frozenset({"run"}), context=ContextViewModel(100, 1000)
    )
    event = _mailbox_event()
    event.data["context"] = {"used_tokens": 999, "limit_tokens": 999}
    state = reduce_event(original, event)
    assert state.notification and "explicit follow-up required" in state.notification
    assert state.context == original.context
    assert state.active_run_id is None
    assert state.activity is None
    assert state.terminal_runs == original.terminal_runs


def test_consumed_mailbox_notice_clears_without_clearing_other_notifications() -> None:
    event = _mailbox_event()
    state = reduce_event(TuiState(), event)
    event.data["mailbox"]["pending_count"] = 0
    event.data["mailbox"]["pending_bytes"] = 0
    assert reduce_event(state, event).notification is None
    waiting = TuiState(notification="Waiting for approval")
    assert reduce_event(waiting, event).notification == "Waiting for approval"


def test_inline_mailbox_notice_preserves_context_and_waiting_activity() -> None:
    async def scenario() -> None:
        output = StringIO()
        state = {"context": (100, 1000), "activity": "Waiting for approval"}
        renderer = _RunRenderer(
            _TerminalWriter(make_console(file=output, force_terminal=False)), state
        )
        await renderer.handle(_mailbox_event())
        assert state["context"] == (100, 1000)
        assert state["activity"] == "Waiting for approval"
        assert "3 pending messages (240 bytes)" in output.getvalue()
        assert "explicit follow-up required" in output.getvalue()
        assert "private agent message" not in output.getvalue()

    asyncio.run(scenario())


def test_mailbox_metadata_rejects_malformed_and_unbounded_diagnostics() -> None:
    assert (
        mailbox_metadata(
            {
                "pending_count": True,
                "pending_bytes": -1,
                "receiver_active": "false",
                "wake_reason": "private message text",
                "notification_latency_ms": float("nan"),
                "delivery_latency_ms": float("inf"),
                "payload": {"text": "private"},
            }
        )
        == {}
    )


def test_mailbox_benchmark_exercises_production_storage_and_reports_bounds(
    tmp_path: Path,
) -> None:
    from scripts.benchmark_mailbox import benchmark

    report = asyncio.run(
        benchmark(tmp_path, history_rows=100, pending_rows=40, iterations=3)
    )
    assert report["history_rows"] == 100
    assert report["pending_rows"] == 40
    assert report["batch"]["count"] == 32
    assert report["batch"]["payload_bytes"] <= 65_536
    assert report["batch"]["has_more"] is True
    assert report["batch"]["actionable_count"] == 24
    assert report["batch"]["passive_count"] == 8
    assert report["reads"]["samples"] == 3
    assert report["reads"]["writes"] == 0
    assert report["notification"]["samples"] == 3
    assert report["notification"]["target_p95_ms"] == 100
    assert report["query_plan"]
    for detail in report["query_plan"]:
        assert "SEARCH agent_mailbox USING INDEX" in detail
        assert "SCAN agent_mailbox" not in detail
