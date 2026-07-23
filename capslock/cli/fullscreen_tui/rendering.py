"""Rich renderables that preserve text styling without painted backgrounds."""

from __future__ import annotations

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.segment import Segment
from rich.style import Style


_TERMINAL_BACKGROUND = Style(bgcolor="default")


class TransparentBackground:
    """Render content on the terminal background without changing font styles."""

    def __init__(self, renderable: RenderableType) -> None:
        self.renderable = renderable

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        for segment in console.render(self.renderable, options):
            if segment.control:
                yield segment
                continue
            style = (segment.style or Style()) + _TERMINAL_BACKGROUND
            yield Segment(segment.text, style)
