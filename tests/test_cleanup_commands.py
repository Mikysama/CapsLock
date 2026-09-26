"""Regressions for the typed command boundary and instruction dispatch."""

import asyncio
import io
from types import SimpleNamespace

from rich.console import Console

from capslock.cli.commands import CommandOutcome, CommandOutcomeKind
from capslock.cli.context import CliContext
from capslock.cli.dispatch import dispatch_slash_command


def test_exit_outcome_does_not_alias_scalar_values():
    outcome = CommandOutcome(CommandOutcomeKind.EXIT)
    assert outcome != False  # noqa: E712 — equality regression, not truthiness
    assert outcome != 0
    assert outcome != "exit"
    assert outcome == CommandOutcome(CommandOutcomeKind.EXIT)
    assert len({outcome, False, "exit"}) == 3


def test_instructions_command_displays_loaded_records():
    output = io.StringIO()
    record = SimpleNamespace(
        loaded=True,
        token_count=12,
        sha256="a" * 64,
        scope="workspace",
        match_rule=None,
        diagnostic=None,
        path="CAPSLOCK.md",
    )
    context = CliContext(
        Console(file=output),
        SimpleNamespace(
            instruction_loader=SimpleNamespace(last=SimpleNamespace(records=[record]))
        ),
    )
    result = asyncio.run(dispatch_slash_command(context, "/instructions list"))
    assert result.kind is CommandOutcomeKind.HANDLED
    assert "CAPSLOCK.md" in output.getvalue()
    assert "tokens=12" in output.getvalue()


def test_controller_shutdown_cancels_queue_without_starting_it():
    from capslock.application.foreground import ForegroundRunController

    async def scenario():
        started = asyncio.Event()
        executions, cancelled = [], []

        class Session:
            memory = None

            async def run_stream(self, request):
                executions.append(request.work_item_id)
                started.set()
                await asyncio.Event().wait()
                yield

            async def cancel_queued_work_item(self, item_id):
                cancelled.append(item_id)

            async def delete_if_empty(self):
                return False

        async def consume(event):
            pass

        controller = ForegroundRunController(Session(), consumer=consume)
        await controller.enqueue_item("active", "first")
        await started.wait()
        await controller.enqueue_item("queued", "second")
        await asyncio.wait_for(controller.shutdown(), 0.2)
        assert executions == ["active"]
        assert cancelled == ["queued", "active"]

    asyncio.run(scenario())


def test_controller_cancel_queued_item_keeps_active_run():
    from capslock.application.foreground import ForegroundRunController

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        executions, cancelled = [], []

        class Session:
            memory = None

            async def run_stream(self, request):
                executions.append(request.work_item_id)
                entered.set()
                await release.wait()
                if False:
                    yield

            async def cancel_queued_work_item(self, item_id):
                cancelled.append(item_id)

            async def delete_if_empty(self):
                return False

        async def consume(event):
            pass

        controller = ForegroundRunController(Session(), consumer=consume)
        try:
            await controller.enqueue_item("first", "first")
            await entered.wait()
            await controller.enqueue_item("second", "second")
            await controller.enqueue_item("second", "second")
            assert await controller.cancel("second")
            assert not await controller.cancel("second")
            release.set()
            await controller.wait_idle()
            assert executions == ["first"]
            assert cancelled == ["second"]
        finally:
            await controller.shutdown()

    asyncio.run(scenario())


def test_controller_shutdown_during_started_notification_never_starts_model():
    from capslock.application.foreground import (
        ControllerEventKind,
        ForegroundRunController,
    )

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        executions, cancelled = [], []

        class Session:
            memory = None

            async def run_stream(self, request):
                executions.append(request.work_item_id)
                if False:
                    yield

            async def cancel_queued_work_item(self, item_id):
                cancelled.append(item_id)

            async def delete_if_empty(self):
                return False

        async def consume(event):
            if event.kind is ControllerEventKind.STARTED:
                entered.set()
                await release.wait()

        controller = ForegroundRunController(Session(), consumer=consume)
        await controller.enqueue_item("item", "question")
        await entered.wait()
        closing = asyncio.create_task(controller.shutdown())
        await asyncio.sleep(0)
        release.set()
        await closing
        assert executions == []
        assert cancelled == ["item"]

    asyncio.run(scenario())
