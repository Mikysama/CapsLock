"""Rich renderables for the scrollback-preserving inline conversation UI."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.markdown import Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.syntax import Syntax
from rich.text import Text

from ...domain import ActionRecord
from ..presentation import ToolPresentation, present_action


@dataclass(frozen=True)
class InlineMessageCard:
    """A background-free Textual-style left border for static scrollback."""

    content: Any
    border_style: str

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        child_options = options.update_width(max(1, options.max_width - 3))
        lines = console.render_lines(self.content, child_options, pad=False)
        style = console.get_style(self.border_style)
        for line in lines:
            yield Segment("▌ ", style)
            yield Segment(" ")
            yield from line
            yield Segment.line()


def user_message(text: str) -> InlineMessageCard:
    return message_card(
        Text.assemble(("❯ ", "user.label"), (text, "user")),
        border_style="border.focus",
    )


def assistant_label() -> Text:
    return Text("◆ CapsLock", style="agent.bold")


def assistant_content(value: str) -> InlineMessageCard:
    return message_card(
        Markdown(value, code_theme="ansi_dark", hyperlinks=True),
        border_style="primary.strong",
    )


def system_message(content: object, *, status: str = "info") -> InlineMessageCard:
    style = {
        "failed": "error",
        "cancelled": "warning",
        "stopped": "warning",
        "waiting": "waiting",
    }.get(status, "border")
    return message_card(content, border_style=style)


def message_card(content: object, *, border_style: str) -> InlineMessageCard:
    """Mirror the Textual message card while retaining terminal scrollback."""

    return InlineMessageCard(content, border_style)


def reasoning_summary(text: str, *, status: str = "complete") -> Text:
    size = len(text.strip())
    suffix = f" · {size} chars" if size else ""
    return Text.assemble(
        ("  ◇ ", "reasoning"),
        (f"Reasoning {status}", "reasoning"),
        (suffix, "text.muted"),
    )


def tool_group(items: list[ToolPresentation], *, expanded: bool) -> Group | Text:
    failed = sum(item.ok is False for item in items)
    marker, style = ("●", "error") if failed else ("●", "success")
    counts = Counter(item.category for item in items)
    kinds = " · ".join(f"{name} {count}" for name, count in counts.items())
    heading = Text.assemble(
        (f"{marker} ", style),
        (f"Explored {len(items)} item(s)", style),
        (f" · {kinds}", "text.muted"),
    )
    if not expanded:
        return heading
    details = [heading]
    details.extend(tool_result(item, compact=True) for item in items)
    return Group(*details)


def tool_result(item: ToolPresentation, *, compact: bool = False) -> Text:
    if item.ok is False:
        marker, style, outcome = "●", "error", "failed"
    elif item.ok is True:
        marker, style, outcome = "●", "success", "done"
    else:
        marker, style, outcome = "◌", "running", "running"
    prefix = "  ↳ " if compact else f"{marker} "
    output = Text(prefix, style=style)
    output.append(item.title, style=style)
    if item.target:
        output.append(f"  {item.target}", style="path")
    if item.detail:
        output.append(f"  {item.detail}", style="text.secondary")
    if item.outcome:
        output.append(f"  {item.outcome}", style="text.muted")
    output.append(f" · {outcome}", style="text.muted")
    if item.duration_ms is not None:
        output.append(f" · {item.duration_ms}ms", style="text.muted")
    return output


def approval_panel(action: ActionRecord) -> Panel:
    value = present_action(action)
    parts: list[object] = [
        Text(value.title, style="text.primary.bold"),
        Text(value.subtitle, style="warning"),
    ]
    if value.target:
        parts.append(Text.assemble(("Target  ", "text.muted"), (value.target, "path")))
    if value.preview:
        lexer = (
            "diff"
            if value.preview_kind == "diff"
            else "bash"
            if value.preview_kind == "command"
            else "text"
        )
        parts.append(
            Syntax(value.preview, lexer, word_wrap=True, background_color="default")
        )
    return Panel(
        Group(*parts),
        title=Text(" Permission required ", style="warning"),
        title_align="left",
        border_style="warning",
        padding=(0, 1),
    )
