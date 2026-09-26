"""Streaming presentation remains lossless while diagnostic logs stay compact."""

import asyncio
import json

import pytest

from capslock.domain import AgentEventKind
from capslock.observability import EventSink
from capslock.runtime.events import RunEventBus


class Journal:
    def __init__(self):
        self.events = []

    async def event_state(self, run_id):
        return 0, "session", "work", "trace"

    async def append_prepared_events(self, events):
        self.events.extend(events)


@pytest.mark.parametrize(
    "terminal", ["completed", "failed", "cancelled", "waiting_input"]
)
def test_deltas_are_summarized_without_changing_live_or_durable_stream(
    tmp_path, terminal
):
    async def scenario():
        journal, live = Journal(), []
        sink = EventSink(tmp_path / "events.jsonl")

        async def consume(event):
            live.append(event)

        bus = RunEventBus(
            run_id="run",
            journal=journal,
            consumer=consume,
            diagnostic=sink.emit,
            diagnostic_queue_size=1,
            flush_interval=60,
            flush_bytes=1000000,
        )
        for _ in range(1000):
            await bus.emit(AgentEventKind.TEXT_DELTA, {"text": "你好"})
        for _ in range(10):
            await bus.emit(AgentEventKind.THINKING, {"text": "reason"})
        await bus.emit(AgentEventKind(terminal), {"answer": "final"})
        await bus.close()
        sink.flush()
        assert len(live) == 1011
        assert (
            "".join(
                e.data["text"]
                for e in journal.events
                if e.kind is AgentEventKind.TEXT_DELTA
            )
            == "你好" * 1000
        )
        rows = [json.loads(line) for line in sink.path.read_text().splitlines()]
        assert len(rows) == 2
        summary, ending = rows
        assert summary["kind"] == "workflow_stream_summary"
        assert summary["data"]["streams"] == {
            "text_delta": {"chunks": 1000, "characters": 2000, "bytes": 6000},
            "thinking": {"chunks": 10, "characters": 60, "bytes": 60},
        }
        assert summary["data"]["first_sequence"] == 1
        assert summary["data"]["last_sequence"] == 1010
        assert "你好" not in json.dumps(summary, ensure_ascii=False)
        assert ending["data"]["event"] == terminal
        assert len(sink.events) == 2

    asyncio.run(scenario())


def test_close_flushes_unfinished_stream_once_and_separates_tool_boundaries(tmp_path):
    async def scenario():
        sink = EventSink(tmp_path / "events.jsonl")

        async def consume(event):
            pass

        bus = RunEventBus(
            run_id="run",
            journal=Journal(),
            consumer=consume,
            diagnostic=sink.emit,
            diagnostic_queue_size=1,
        )
        await bus.emit(AgentEventKind.TEXT_DELTA, {"text": "partial"})
        await bus.emit(AgentEventKind.TOOL_RUNNING, {"name": "read_file"})
        await bus.emit(AgentEventKind.THINKING, {"text": "unfinished"})
        await bus.close()
        await bus.close()
        sink.flush()
        rows = [json.loads(line) for line in sink.path.read_text().splitlines()]
        assert [r["kind"] for r in rows] == [
            "workflow_stream_summary",
            "workflow_event",
            "workflow_stream_summary",
        ]
        assert rows[0]["data"]["streams"]["text_delta"]["chunks"] == 1
        assert rows[2]["data"]["streams"]["thinking"]["chunks"] == 1
        assert rows[2]["data"]["last_sequence"] == 3

    asyncio.run(scenario())


def test_diagnostic_sink_failure_does_not_break_live_stream():
    async def scenario():
        live = []

        async def consume(event):
            live.append(event)

        def broken(*args, **kwargs):
            raise OSError("disk full")

        bus = RunEventBus(
            run_id="run",
            journal=Journal(),
            consumer=consume,
            diagnostic=broken,
            diagnostic_queue_size=1,
        )
        await bus.emit(AgentEventKind.TEXT_DELTA, {"text": "partial"})
        await bus.emit(AgentEventKind.FAILED, {"error": "model error"})
        await bus.close()
        assert len(live) == 2

    asyncio.run(scenario())


def test_thinking_start_and_persisted_terminal_remain_visible(tmp_path):
    from dataclasses import replace

    async def scenario():
        sink = EventSink(tmp_path / "events.jsonl")
        live = []

        async def consume(event):
            live.append(event)

        bus = RunEventBus(
            run_id="run",
            journal=Journal(),
            consumer=consume,
            diagnostic=sink.emit,
            diagnostic_queue_size=1,
        )
        start = await bus.emit(AgentEventKind.THINKING, {})
        await bus.emit(AgentEventKind.TEXT_DELTA, {"text": "answer"})
        terminal = replace(
            start,
            sequence=3,
            kind=AgentEventKind.WAITING_INPUT,
            data={"request_id": "input"},
        )
        await bus.publish_persisted(terminal)
        await bus.close()
        # A terminal writes the complete buffered log, without a separate sink flush.
        rows = [json.loads(line) for line in sink.path.read_text().splitlines()]
        assert len(rows) == 3
        assert rows[0]["data"]["event"] == "thinking"
        assert rows[1]["kind"] == "workflow_stream_summary"
        assert rows[2]["data"]["event"] == "waiting_input"
        assert live[-1] is terminal

    asyncio.run(scenario())
