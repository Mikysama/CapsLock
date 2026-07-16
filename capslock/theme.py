"""Transparent blue-gray terminal theme shared by Rich and prompt_toolkit."""

from __future__ import annotations

import os
from collections.abc import Mapping

from prompt_toolkit.styles import Style as PromptStyle
from rich.color import Color, ColorSystem
from rich.console import Console
from rich.style import Style as RichStyle
from rich.theme import Theme


THEME_TOKENS: Mapping[str, str] = {
    "background": "transparent",
    "surface": "transparent",
    "overlay": "transparent",
    "textPrimary": "#DCE6F2",
    "textSecondary": "#A9B8C8",
    "textMuted": "#718397",
    "textDisabled": "#4D5C6D",
    "primary": "#7FA7C9",
    "primaryStrong": "#5F8FB8",
    "primarySoft": "#9DBBD3",
    "accent": "#6F9FC5",
    "border": "#52687C",
    "borderMuted": "#3D4F61",
    "borderFocus": "#8CB9DC",
    "thinking": "#9A8FC7",
    "running": "#72A7CC",
    "waiting": "#C4A96B",
    "success": "#7FAF9A",
    "warning": "#C4A96B",
    "error": "#C77F86",
    "info": "#72A7CC",
    "user": "#C7D3DF",
    "agent": "#8FB6D6",
    "tool": "#7D9EB8",
    "command": "#A8C6DD",
    "path": "#89AFC8",
    "code": "#C2CEDA",
}


RICH_STYLE_DEFINITIONS: Mapping[str, str] = {
    "text.primary": THEME_TOKENS["textPrimary"],
    "text.secondary": THEME_TOKENS["textSecondary"],
    "text.muted": THEME_TOKENS["textMuted"],
    "text.disabled": THEME_TOKENS["textDisabled"],
    "primary": THEME_TOKENS["primary"],
    "primary.strong": THEME_TOKENS["primaryStrong"],
    "primary.soft": THEME_TOKENS["primarySoft"],
    "accent": THEME_TOKENS["accent"],
    "border": THEME_TOKENS["border"],
    "border.muted": THEME_TOKENS["borderMuted"],
    "border.focus": THEME_TOKENS["borderFocus"],
    "thinking": THEME_TOKENS["thinking"],
    "running": THEME_TOKENS["running"],
    "waiting": THEME_TOKENS["waiting"],
    "success": THEME_TOKENS["success"],
    "warning": THEME_TOKENS["warning"],
    "error": THEME_TOKENS["error"],
    "info": THEME_TOKENS["info"],
    "user": THEME_TOKENS["user"],
    "agent": THEME_TOKENS["agent"],
    "tool": THEME_TOKENS["tool"],
    "command": THEME_TOKENS["command"],
    "path": THEME_TOKENS["path"],
    "code": THEME_TOKENS["code"],
}
RICH_BOLD_STYLE_DEFINITIONS: Mapping[str, str] = {
    "text.primary.bold": THEME_TOKENS["textPrimary"],
    "primary.bold": THEME_TOKENS["primary"],
    "primary.soft.bold": THEME_TOKENS["primarySoft"],
    "agent.bold": THEME_TOKENS["agent"],
    "command.bold": THEME_TOKENS["command"],
    "error.bold": THEME_TOKENS["error"],
    "thinking.bold": THEME_TOKENS["thinking"],
    "running.bold": THEME_TOKENS["running"],
    "waiting.bold": THEME_TOKENS["waiting"],
}


def _make_rich_theme(color_system: object = None) -> Theme:
    # Pre-downgrade explicit modes so Rich's cross-console style cache remains safe.
    target = {
        "standard": ColorSystem.STANDARD,
        "256": ColorSystem.EIGHT_BIT,
        "truecolor": ColorSystem.TRUECOLOR,
        "windows": ColorSystem.WINDOWS,
    }.get(color_system)
    styles = {}
    for name, value in RICH_STYLE_DEFINITIONS.items():
        color = Color.parse(value)
        if target is not None:
            color = color.downgrade(target)
        styles[name] = RichStyle(color=color)
    for name, value in RICH_BOLD_STYLE_DEFINITIONS.items():
        color = Color.parse(value)
        if target is not None:
            color = color.downgrade(target)
        styles[name] = RichStyle(color=color, bold=True)
    return Theme(styles)


RICH_THEME = _make_rich_theme()


def no_color_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Follow the NO_COLOR convention: presence of the variable disables color."""
    return "NO_COLOR" in (os.environ if environ is None else environ)


def make_console(**kwargs: object) -> Console:
    """Create a transparent console that degrades through Rich's color system."""
    # Rich Style caches rendered ANSI codes, so each color system gets a fresh theme.
    kwargs.setdefault("theme", _make_rich_theme(kwargs.get("color_system")))
    kwargs.setdefault("style", "text.primary")
    kwargs.setdefault("no_color", no_color_enabled())
    return Console(**kwargs)


def build_prompt_style(*, no_color: bool | None = None) -> PromptStyle:
    """Create the matching prompt style without applying component backgrounds."""
    use_color = not (no_color_enabled() if no_color is None else no_color)

    def foreground(token: str, *attributes: str) -> str:
        parts = [*attributes]
        if use_color:
            parts.append(THEME_TOKENS[token])
        return " ".join(parts)

    def transparent_foreground(token: str, *attributes: str) -> str:
        return f"bg:default {foreground(token, *attributes)}".rstrip()

    return PromptStyle.from_dict(
        {
            "permission": foreground("waiting", "bold"),
            "prompt": foreground("primary", "bold"),
            "input-border": foreground("borderFocus"),
            "footer": foreground("textMuted"),
            "slash-command": foreground("command", "bold"),
            "user-input": foreground("user"),
            "command-name": foreground("command", "bold"),
            "completion-menu": "bg:default",
            "completion-menu.completion": transparent_foreground("textPrimary"),
            "completion-menu.completion.current": transparent_foreground("primarySoft", "bold"),
            "completion-menu.meta.completion": transparent_foreground("textSecondary"),
            "completion-menu.meta.completion.current": transparent_foreground("textPrimary", "bold"),
            "input-selection": "bg:default",
            "option": transparent_foreground("textPrimary"),
            "selected-option": transparent_foreground("primarySoft", "bold"),
            "number": transparent_foreground("textMuted"),
            "bottom-toolbar": transparent_foreground("textMuted", "noreverse"),
            "scrollbar.background": "bg:default",
            "scrollbar.button": transparent_foreground("textDisabled"),
        }
    )
