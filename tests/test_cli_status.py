from __future__ import annotations

import asyncio
import io

import pytest

from capslock.cli.context import CliContext
from capslock.cli.app import build_parser
from capslock.cli.exec import run_exec
from capslock.cli.status import AsyncStatusRenderer
from capslock.domain import AgentEvent, AgentEventKind
from capslock.status import AgentStatus
from capslock.theme import make_console


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class EventAgent:
    def __init__(self, events: list[AgentEvent]) -> None:
        self.events = events

    async def run_stream(self, request):
        for item in self.events:
            yield item


class BlockingAgent:
    async def run_stream(self, request):
        yield _event(AgentEventKind.THINKING)
        await asyncio.Event().wait()


def _event(
    kind: AgentEventKind, data: dict[str, object] | None = None, sequence: int = 1
) -> AgentEvent:
    return AgentEvent(
        sequence,
        "2026-01-01T00:00:00+00:00",
        "session",
        "run",
        "work",
        kind,
        data or {},
    )


def _renderer(stream: io.StringIO, **kwargs) -> AsyncStatusRenderer:
    return AsyncStatusRenderer(
        stream,
        interval=0.001,
        width_provider=lambda: 80,
        environ={},
        **kwargs,
    )


def test_status_start_update_stop_and_repeated_stop() -> None:
    async def scenario() -> None:
        stream = TTYBuffer()
        renderer = _renderer(stream)
        await renderer.start()
        assert renderer.running
        await renderer.update(AgentStatus.TOOL_CALLING, "shell")
        assert renderer.status is AgentStatus.TOOL_CALLING
        assert "Running tool: shell..." in stream.getvalue()
        await asyncio.sleep(0.003)
        await renderer.stop(AgentStatus.DONE)
        await renderer.stop(AgentStatus.DONE)
        assert not renderer.running
        output = stream.getvalue()
        assert "\x1b[?25l" in output
        assert "\x1b[?25h" in output
        assert output.count("✓ Done\n") == 1

    asyncio.run(scenario())


def test_exception_context_restores_cursor_without_swallowing_error() -> None:
    async def scenario() -> None:
        stream = TTYBuffer()
        renderer = _renderer(stream)
        with pytest.raises(RuntimeError, match="business failed"):
            async with renderer:
                raise RuntimeError("business failed")
        assert "\x1b[?25h" in stream.getvalue()
        assert "✗ Failed" in stream.getvalue()

    asyncio.run(scenario())


def test_start_failure_after_hiding_cursor_restores_it() -> None:
    async def scenario() -> None:
        stream = TTYBuffer()

        def fail_width() -> int:
            raise RuntimeError("terminal query failed")

        renderer = AsyncStatusRenderer(
            stream, interval=1, environ={}, width_provider=fail_width
        )
        with pytest.raises(RuntimeError, match="terminal query failed"):
            await renderer.start()
        assert stream.getvalue().endswith("\r\x1b[2K\x1b[?25h")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("stream", "enabled", "environ"),
    [
        (io.StringIO(), True, {}),
        (TTYBuffer(), False, {}),
        (TTYBuffer(), True, {"CI": "true"}),
        (TTYBuffer(), True, {"TERM": "dumb"}),
    ],
)
def test_disabled_or_non_tty_status_emits_nothing(
    stream: io.StringIO, enabled: bool, environ: dict[str, str]
) -> None:
    async def scenario() -> None:
        renderer = AsyncStatusRenderer(stream, enabled=enabled, environ=environ)
        await renderer.start()
        await renderer.update(AgentStatus.READING, "read_file")
        await renderer.stop(AgentStatus.DONE)
        assert stream.getvalue() == ""

    asyncio.run(scenario())


def test_no_spinner_cli_flag_is_exposed() -> None:
    args = build_parser().parse_args(["exec", "question", "--no-spinner"])
    assert args.no_spinner is True
    assert build_parser().parse_args(["--no-spinner"]).no_spinner is True
    assert (
        build_parser().parse_args(["--no-spinner", "exec", "question"]).no_spinner
        is True
    )
    assert build_parser().parse_args(["resume", "--no-spinner"]).no_spinner is True


def test_first_token_clears_spinner_once_and_stdout_only_contains_answer() -> None:
    async def scenario() -> None:
        stdout, stderr = io.StringIO(), TTYBuffer()
        console = make_console(
            file=stdout, force_terminal=False, color_system=None, width=80
        )
        events = [
            _event(AgentEventKind.THINKING, sequence=1),
            _event(AgentEventKind.TEXT_DELTA, {"text": "Hello "}, 2),
            _event(AgentEventKind.TEXT_DELTA, {"text": "world"}, 3),
            _event(
                AgentEventKind.COMPLETED,
                {"status": "completed", "answer": "Hello world"},
                4,
            ),
        ]
        renderer = _renderer(stderr, output_is_tty=True)
        result = await run_exec(
            CliContext(console, EventAgent(events)),
            "question",
            status_renderer=renderer,
        )
        assert result == 0
        assert stdout.getvalue() == "Hello world\n"
        assert "\x1b[" not in stdout.getvalue()
        assert stderr.getvalue().count("\x1b[?25h") == 1
        assert stderr.getvalue().count("✓ Done\n") == 1

    asyncio.run(scenario())


def test_failed_status_and_error_stay_out_of_stdout() -> None:
    async def scenario() -> None:
        stdout, stderr = io.StringIO(), TTYBuffer()
        console = make_console(
            file=stdout, force_terminal=False, color_system=None, width=80
        )
        result = await run_exec(
            CliContext(
                console,
                EventAgent(
                    [
                        _event(AgentEventKind.THINKING, sequence=1),
                        _event(
                            AgentEventKind.FAILED,
                            {
                                "status": "failed",
                                "error": {"message": "transport failed"},
                            },
                            2,
                        ),
                    ]
                ),
            ),
            "question",
            status_renderer=_renderer(stderr, output_is_tty=True),
        )
        assert result == 1
        assert stdout.getvalue() == ""
        assert "✗ Failed" in stderr.getvalue()
        assert "transport failed" in stderr.getvalue()

    asyncio.run(scenario())


def test_tool_events_switch_status_and_do_not_duplicate_streamed_text() -> None:
    async def scenario() -> None:
        stdout, stderr = io.StringIO(), TTYBuffer()
        console = make_console(
            file=stdout, force_terminal=False, color_system=None, width=80
        )
        events = [
            _event(AgentEventKind.THINKING, sequence=1),
            _event(AgentEventKind.TOOL_RUNNING, {"name": "read_file"}, 2),
            _event(AgentEventKind.TOOL_COMPLETED, {"name": "read_file", "ok": True}, 3),
            _event(AgentEventKind.THINKING, sequence=4),
            _event(AgentEventKind.TEXT_DELTA, {"text": "result"}, 5),
            _event(
                AgentEventKind.COMPLETED,
                {"status": "completed", "answer": "result"},
                6,
            ),
        ]
        await run_exec(
            CliContext(console, EventAgent(events)),
            "question",
            status_renderer=_renderer(stderr, output_is_tty=True),
        )
        assert stdout.getvalue() == "result\n"
        status_output = stderr.getvalue()
        assert "Reading files: read_file..." in status_output
        assert "Analyzing results..." in status_output

    asyncio.run(scenario())


def test_cancellation_cleans_spinner_and_restores_cursor() -> None:
    async def scenario() -> None:
        stdout, stderr = io.StringIO(), TTYBuffer()
        console = make_console(file=stdout, force_terminal=False, color_system=None)
        renderer = _renderer(stderr, output_is_tty=True)
        task = asyncio.create_task(
            run_exec(
                CliContext(console, BlockingAgent()),
                "question",
                status_renderer=renderer,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "\x1b[?25h" in stderr.getvalue()
        assert stderr.getvalue().count("! Cancelled\n") == 1

    asyncio.run(scenario())


def test_long_unicode_status_is_truncated_to_terminal_width() -> None:
    async def scenario() -> None:
        stream = TTYBuffer()
        renderer = AsyncStatusRenderer(
            stream,
            interval=1,
            environ={},
            width_provider=lambda: 18,
        )
        await renderer.start(AgentStatus.TOOL_CALLING, "读取非常长的文件名称")
        await renderer.stop()
        drawn = stream.getvalue().split("\x1b[2K", 1)[1].split("\r", 1)[0]
        from prompt_toolkit.utils import get_cwidth

        assert get_cwidth(drawn) <= 18

    asyncio.run(scenario())


def test_completed_without_delta_falls_back_to_answer_and_reuses_renderer() -> None:
    async def scenario() -> None:
        stdout, stderr = io.StringIO(), TTYBuffer()
        console = make_console(
            file=stdout, force_terminal=False, color_system=None, width=80
        )
        renderer = _renderer(stderr, output_is_tty=True)
        for answer in ("first", "second"):
            events = [
                _event(AgentEventKind.THINKING, sequence=1),
                _event(
                    AgentEventKind.COMPLETED,
                    {"status": "completed", "answer": answer},
                    2,
                ),
            ]
            assert (
                await run_exec(
                    CliContext(console, EventAgent(events)),
                    "question",
                    status_renderer=renderer,
                )
                == 0
            )
        assert stdout.getvalue() == "first\nsecond\n"
        status_output = stderr.getvalue()
        assert status_output.count("\x1b[?25h") == 2
        assert status_output.count("✓ Done\n") == 2

    asyncio.run(scenario())
