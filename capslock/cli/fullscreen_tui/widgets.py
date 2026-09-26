"""Reusable Textual widgets for the fullscreen TUI."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from math import ceil

from rich.cells import cell_len
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static, TextArea

from ...status import SPINNER_FRAMES
from ...theme import terminal_style
from .models import MessageKind, MessageViewModel, QueueViewModel, TuiState
from .rendering import TransparentBackground


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

    class RecallRequested(Message):
        pass

    class HelpRequested(Message):
        pass

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
        elif (
            event.key == "up"
            and "\n" not in self.text
            and self.cursor_location == (0, 0)
        ):
            event.prevent_default()
            event.stop()
            self.post_message(self.HistoryRequested(-1))
        elif event.key == "down" and "\n" not in self.text:
            event.prevent_default()
            event.stop()
            self.post_message(self.HistoryRequested(1))
        elif event.key == "escape" and not self.text:
            event.prevent_default()
            event.stop()
            self.post_message(self.RecallRequested())
        elif event.character == "?" and not self.text:
            event.prevent_default()
            event.stop()
            self.post_message(self.HelpRequested())

    def fit_height(self, *, width: int, terminal_height: int) -> None:
        content_width = max(1, width - 4)
        visual_lines = sum(
            max(1, ceil(cell_len(line) / content_width))
            for line in (self.text.splitlines() or [""])
        )
        maximum = 5 if terminal_height < 24 else 8
        self.styles.height = max(3, min(maximum, visual_lines + 2))


class MessageWidget(Static):
    def __init__(self, message: MessageViewModel) -> None:
        super().__init__(classes=f"message {message.kind.value}")
        self.message_id = message.id
        self._message: MessageViewModel | None = None
        self.update_message(message)

    def update_message(self, message: MessageViewModel) -> None:
        if message == self._message:
            return
        self._message = message
        self.set_classes(f"message {message.kind.value}")
        if message.kind is MessageKind.ASSISTANT:
            self.update(TransparentBackground(RichMarkdown(message.text or " ")))
            return
        if message.kind is MessageKind.USER:
            self.update(
                Text.assemble(
                    ("❯ ", terminal_style("userPromptAccent", "bold")),
                    message.text,
                )
            )
            return
        if message.kind is MessageKind.REASONING:
            text = message.text.strip()
            if message.collapsed:
                first = " ".join(text.split())[:120]
                self.update(
                    Text.assemble(
                        ("◇ Reasoning ", terminal_style("reasoning", "italic")),
                        (first + ("…" if len(text) > 120 else ""), "dim"),
                    )
                )
            else:
                self.update(
                    Text.assemble(
                        ("◇ Reasoning\n", terminal_style("thinking", "italic")),
                        (text, "italic dim"),
                    )
                )
            return
        if message.kind is MessageKind.TOOLS:
            self.update(_tool_text(message))
            return
        style = {
            "failed": terminal_style("error"),
            "cancelled": terminal_style("warning"),
            "stopped": terminal_style("warning"),
            "waiting_approval": terminal_style("waiting"),
        }.get(message.status or "", terminal_style("textSecondary"))
        self.update(Text(message.text, style=style))


def _tool_text(message: MessageViewModel) -> Text:
    output = Text()
    if message.collapsed:
        running = sum(tool.status == "running" for tool in message.tools)
        failed = sum(tool.status == "failed" for tool in message.tools)
        counts = Counter(tool.category for tool in message.tools)
        kinds = " · ".join(f"{name} {count}" for name, count in counts.items())
        marker = "●" if not running else "◌"
        style = (
            terminal_style("error")
            if failed
            else terminal_style("running")
            if running
            else terminal_style("success")
        )
        output.append(f"{marker} Explored {len(message.tools)} · {kinds}", style=style)
        if running:
            output.append(f" · {running} running", style=terminal_style("running"))
        if failed:
            output.append(f" · {failed} failed", style=terminal_style("error"))
        output.append("  Ctrl+O to expand", style="dim")
        return output
    for index, tool in enumerate(message.tools):
        if index:
            output.append("\n")
        marker, style = {
            "queued": ("○", terminal_style("waiting")),
            "running": ("◌", terminal_style("running")),
            "waiting_approval": ("◌", terminal_style("waiting")),
            "success": ("●", terminal_style("success")),
            "failed": ("●", terminal_style("error")),
            "cancelled": ("●", terminal_style("warning")),
        }.get(tool.status, ("○", terminal_style("textSecondary")))
        output.append(f"{marker} {tool.title}", style=style)
        if tool.target:
            output.append(f"  {tool.target}", style=terminal_style("path"))
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
        del workspace, model, permission, width
        self.update(
            Text.assemble(
                ("⇪ CapsLock", terminal_style("borderFocus", "bold")),
                (f" · {title}", "bold"),
            )
        )


class QueueBar(Static):
    def update_queue(self, items: tuple[QueueViewModel, ...]) -> None:
        if not items:
            self.display = False
            self.update("")
            return
        self.display = True
        latest = items[-1]
        preview = " ".join(latest.text.split())[:60]
        output = Text("Queued ", style=terminal_style("waiting", "bold"))
        output.append(f"{len(items)} · {preview}")
        output.append(" · ↑ edit", style="dim")
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
            output.append(
                marker,
                style=(
                    terminal_style("borderFocus", "bold")
                    if index == selected
                    else "dim"
                ),
            )
            output.append(
                name.ljust(name_width),
                style=(
                    terminal_style("textPrimary", "bold")
                    if index == selected
                    else terminal_style("command", "bold")
                ),
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
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.frame = 0
        self._clear = True

    def update_state(self, state: TuiState, *, enabled: bool) -> bool:
        active = bool(enabled and state.activity and not state.has_streaming_answer)
        if active:
            glyph = SPINNER_FRAMES[self.frame % len(SPINNER_FRAMES)]
            self.frame += 1
            self.update(
                Text(
                    f"{glyph} {state.activity}…",
                    style=terminal_style("running", "bold"),
                )
            )
            self._clear = False
        elif not self._clear:
            self.update(" ")
            self._clear = True
        return active


class StatusBar(Static):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.frame = 0

    def update_status(
        self,
        state: TuiState,
        *,
        model: str,
        permission: str,
        workspace: str,
        width: int,
        context_limit: int,
    ) -> bool:
        usage = state.usage
        context = _context_label(
            state.context.used_tokens, state.context.limit_tokens or context_limit
        )
        activity = state.activity
        if activity:
            glyph = SPINNER_FRAMES[self.frame % len(SPINNER_FRAMES)]
            self.frame += 1
            activity = f"{glyph} {activity}…"
        if width < 72:
            value = activity or f"{permission} · {context}"
        elif usage.source in {"unknown", "partial"}:
            value = f"{model} · {permission} · {context} · usage unknown"
            value = f"{activity} · {value}" if activity else value
        elif width < 100:
            status = (
                f"{model} · {permission} · {context} · "
                f"turn {usage.input_tokens + usage.output_tokens} tok"
            )
            value = f"{activity} · {status}" if activity else status
        else:
            status = (
                f"{workspace} · {model} · {permission} · {context} · "
                f"turn {usage.input_tokens}/{usage.output_tokens} tok · "
                f"${usage.cost_usd:.4f}"
            )
            value = f"{activity} · {status}" if activity else status
        self.update(Text(value, style=terminal_style("textMuted")))
        return bool(activity)


def _context_label(used: int | None, limit: int) -> str:
    limit_label = _compact_tokens(limit)
    if used is None:
        return f"ctx —/{limit_label}"
    percent = used * 100 / max(1, limit)
    return f"ctx {_compact_tokens(used)}/{limit_label} ({percent:.1f}%)"


def _compact_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


class BottomArea(Vertical):
    def compose(self) -> ComposeResult:
        yield QueueBar(id="queue-bar")
        yield Composer(
            id="composer",
            soft_wrap=True,
            show_line_numbers=False,
            placeholder="Ask CapsLock…  / for commands · $ for Skills",
        )
        yield StatusBar(id="status")
