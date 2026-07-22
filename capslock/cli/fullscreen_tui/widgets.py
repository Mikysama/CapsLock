"""Reusable Textual widgets for the fullscreen TUI."""

from __future__ import annotations

from collections.abc import Iterable

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static, TextArea

from ...status import SPINNER_FRAMES
from .models import MessageKind, MessageViewModel, QueueViewModel, TuiState


class Composer(TextArea):
    """Multiline editor with explicit submit and history messages."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class HistoryRequested(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    class CompletionRequested(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    completion_count = 0

    async def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
        elif event.key in {"ctrl+j", "ctrl+enter", "shift+enter"}:
            event.prevent_default()
            event.stop()
            self.insert("\n")
        elif event.key in {"up", "down"} and self.completion_count:
            event.prevent_default()
            event.stop()
            self.post_message(self.CompletionRequested(-1 if event.key == "up" else 1))
        elif event.key == "up" and "\n" not in self.text and self.cursor_location == (0, 0):
            event.prevent_default()
            event.stop()
            self.post_message(self.HistoryRequested(-1))
        elif event.key == "down" and "\n" not in self.text:
            event.prevent_default()
            event.stop()
            self.post_message(self.HistoryRequested(1))


class MessageWidget(Static):
    def __init__(self, message: MessageViewModel) -> None:
        super().__init__(classes=f"message {message.kind.value}")
        self.message_id = message.id
        self.update_message(message)

    def update_message(self, message: MessageViewModel) -> None:
        self.set_classes(f"message {message.kind.value}")
        if message.kind is MessageKind.ASSISTANT:
            self.update(RichMarkdown(message.text or " "))
            return
        if message.kind is MessageKind.USER:
            self.update(Text.assemble(("❯ ", "bold #8CB9DC"), message.text))
            return
        if message.kind is MessageKind.REASONING:
            text = message.text.strip()
            if message.collapsed:
                first = " ".join(text.split())[:120]
                self.update(Text.assemble(("◇ Reasoning ", "italic #718397"), (first + ("…" if len(text) > 120 else ""), "dim")))
            else:
                self.update(Text.assemble(("◇ Reasoning\n", "italic #9A8FC7"), (text, "italic dim")))
            return
        if message.kind is MessageKind.TOOLS:
            self.update(_tool_text(message))
            return
        style = {
            "failed": "#C77F86",
            "cancelled": "#C4A96B",
            "stopped": "#C4A96B",
            "waiting_approval": "#C4A96B",
        }.get(message.status or "", "#A9B8C8")
        self.update(Text(message.text, style=style))


def _tool_text(message: MessageViewModel) -> Text:
    output = Text()
    if message.collapsed:
        running = sum(tool.status == "running" for tool in message.tools)
        failed = sum(tool.status == "failed" for tool in message.tools)
        marker = "●" if not running else "◌"
        style = "#C77F86" if failed else "#72A7CC" if running else "#7FAF9A"
        output.append(f"{marker} Read/search · {len(message.tools)} tools", style=style)
        if failed:
            output.append(f" · {failed} failed", style="#C77F86")
        output.append("  Ctrl+O to expand", style="dim")
        return output
    for index, tool in enumerate(message.tools):
        if index:
            output.append("\n")
        marker, style = {
            "running": ("◌", "#72A7CC"),
            "success": ("●", "#7FAF9A"),
            "failed": ("●", "#C77F86"),
            "cancelled": ("●", "#C4A96B"),
        }.get(tool.status, ("○", "#A9B8C8"))
        output.append(f"{marker} {tool.title}", style=style)
        if tool.target:
            output.append(f"  {tool.target}", style="#89AFC8")
        if tool.detail:
            output.append(f"  {tool.detail}", style="dim")
        if tool.duration_ms is not None:
            output.append(f"  {tool.duration_ms}ms", style="dim")
    return output


class TranscriptView(VerticalScroll):
    """Message list that updates existing widgets during streaming."""

    PAGE_SIZE = 100

    class LoadOlder(Message):
        pass

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._message_ids: list[str] = []
        self._start_index: int | None = None
        self._total_messages = 0

    async def sync_messages(
        self,
        messages: tuple[MessageViewModel, ...],
        *,
        follow: bool = False,
    ) -> None:
        self._total_messages = len(messages)
        if self._start_index is None or follow:
            self._start_index = max(0, len(messages) - self.PAGE_SIZE)
        else:
            self._start_index = min(self._start_index, len(messages))
        visible = messages[self._start_index :]
        identifiers = [item.id for item in visible]
        if identifiers == self._message_ids and len(self.children) == len(visible):
            for widget, message in zip(self.children, visible):
                if isinstance(widget, MessageWidget):
                    widget.update_message(message)
        elif (
            self._message_ids
            and identifiers[: len(self._message_ids)] == self._message_ids
            and len(self.children) == len(self._message_ids)
        ):
            await self.mount(
                *[MessageWidget(item) for item in visible[len(self._message_ids) :]]
            )
            self._message_ids = identifiers
        else:
            await self.remove_children()
            if visible:
                await self.mount(*[MessageWidget(item) for item in visible])
            self._message_ids = identifiers

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        start = self._start_index or 0
        if self.scroll_y <= 0 and start > 0:
            event.stop()
            self._start_index = max(0, start - self.PAGE_SIZE)
            self.post_message(self.LoadOlder())


class SessionHeader(Static):
    def update_header(
        self,
        *,
        title: str,
        workspace: str,
        model: str,
        permission: str,
        width: int,
    ) -> None:
        if width < 72:
            self.update(Text.assemble(("⇪ CapsLock", "bold #8CB9DC"), (f" · {title}", "#DCE6F2")))
        elif width < 100:
            self.update(Text.assemble(("⇪ CapsLock  ", "bold #8CB9DC"), (title, "bold"), (f"\n{model} · {permission}", "dim")))
        else:
            self.update(Text.assemble(("⇪ CapsLock  ", "bold #8CB9DC"), (title, "bold"), (f"\n{workspace}  ·  {model}  ·  {permission}", "dim")))


class QueueBar(Static):
    def update_queue(self, items: tuple[QueueViewModel, ...]) -> None:
        if not items:
            self.display = False
            self.update("")
            return
        self.display = True
        output = Text("Queue  ", style="bold #C4A96B")
        for index, item in enumerate(items):
            if index:
                output.append("  ·  ", style="dim")
            output.append(f"{item.id[:8]} {item.status}: {item.text[:40]}")
        self.update(output)


class CompletionBar(VerticalScroll):
    """Scrollable command/Skill menu rendered as one candidate per row."""

    def compose(self) -> ComposeResult:
        yield Static(classes="completion-content")

    def update_candidates(
        self, candidates: Iterable[tuple[str, str]], *, selected: int = 0
    ) -> None:
        items = list(candidates)
        if not items:
            self.display = False
            self.query_one(".completion-content", Static).update("")
            self.scroll_home(animate=False)
            return
        self.display = True
        output = Text()
        name_width = max(len(name) for name, _description in items)
        for index, (name, description) in enumerate(items):
            if index:
                output.append("\n")
            marker = "❯ " if index == selected else "  "
            output.append(marker, style="bold #8CB9DC" if index == selected else "dim")
            output.append(
                name.ljust(name_width),
                style="bold #DCE6F2" if index == selected else "bold #A8C6DD",
            )
            output.append(f"  {description}", style="dim")
        self.query_one(".completion-content", Static).update(output)
        self.call_after_refresh(self._reveal_candidate, selected)

    def _reveal_candidate(self, selected: int) -> None:
        """Keep keyboard navigation visible without snapping unnecessarily."""

        height = max(1, self.content_region.height)
        if selected < self.scroll_y:
            self.scroll_to(y=selected, animate=False)
        elif selected >= self.scroll_y + height:
            self.scroll_to(y=selected - height + 1, animate=False)


class ActivityBar(Static):
    frame = 0

    def update_state(self, state: TuiState, *, enabled: bool) -> None:
        if enabled and state.activity and not state.has_streaming_answer:
            glyph = SPINNER_FRAMES[self.frame % len(SPINNER_FRAMES)]
            self.frame += 1
            self.update(Text(f"{glyph} {state.activity}…", style="bold #72A7CC"))
        else:
            self.update(" ")


class StatusBar(Static):
    def update_status(
        self,
        state: TuiState,
        *,
        model: str,
        permission: str,
        workspace: str,
        width: int,
        context_limit: int,
    ) -> None:
        usage = state.usage
        context_messages = sum(
            message.kind in {MessageKind.USER, MessageKind.ASSISTANT}
            for message in state.messages
        )
        if width < 72:
            value = f"{permission} · {usage.input_tokens + usage.output_tokens} tok"
        elif width < 100:
            value = (
                f"{model} · {permission} · ctx {context_messages}/{context_limit} · "
                f"{usage.input_tokens}/{usage.output_tokens} tok · ${usage.cost_usd:.4f}"
            )
        else:
            value = (
                f"{workspace}  ·  {model}  ·  {permission}  ·  "
                f"ctx {context_messages}/{context_limit}  ·  "
                f"{usage.input_tokens}/{usage.output_tokens} tok  ·  "
                f"${usage.cost_usd:.4f}"
            )
        self.update(Text(value, style="#718397"))


class BottomArea(Vertical):
    def compose(self) -> ComposeResult:
        yield QueueBar(id="queue-bar")
        yield CompletionBar(id="completions")
        yield Composer(
            id="composer",
            soft_wrap=True,
            show_line_numbers=False,
            placeholder="Ask CapsLock…  / for commands · $ for Skills",
        )
        yield ActivityBar(id="activity")
        yield StatusBar(id="status")
