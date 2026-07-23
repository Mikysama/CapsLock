from io import StringIO
import re

from capslock.cli.views.conversation import assistant_content
from capslock.theme import (
    RICH_BOLD_STYLE_DEFINITIONS,
    RICH_STYLE_DEFINITIONS,
    RICH_THEME,
    THEME_TOKENS,
    build_prompt_style,
    make_console,
    no_color_enabled,
)


def _render(color_system: str | None, *, no_color: bool = False) -> str:
    stream = StringIO()
    terminal = make_console(
        file=stream,
        color_system=color_system,
        force_terminal=True,
        no_color=no_color,
    )
    terminal.print("[primary]CapsLock[/] [text.secondary]ready[/]")
    return stream.getvalue()


def test_theme_declares_transparent_layers_and_requested_tokens() -> None:
    assert THEME_TOKENS["background"] == "transparent"
    assert THEME_TOKENS["surface"] == "transparent"
    assert THEME_TOKENS["overlay"] == "transparent"
    assert THEME_TOKENS["textPrimary"] == "#DCE6F2"
    assert THEME_TOKENS["borderFocus"] == "#8CB9DC"
    assert THEME_TOKENS["agent"] == "#8FB6D6"
    assert THEME_TOKENS["reasoning"] != THEME_TOKENS["answer"]
    assert RICH_BOLD_STYLE_DEFINITIONS["user.label"] == THEME_TOKENS["accent"]
    assert RICH_THEME.styles["user.label"].bold
    assert RICH_THEME.styles["user.label"].color != RICH_THEME.styles["user"].color
    assert RICH_THEME.styles["reasoning"].dim
    assert RICH_THEME.styles["reasoning"].italic
    assert not RICH_THEME.styles["answer"].dim
    assert not RICH_THEME.styles["answer"].italic
    component_styles = set(RICH_STYLE_DEFINITIONS) | set(RICH_BOLD_STYLE_DEFINITIONS)
    assert all(RICH_THEME.styles[name].bgcolor is None for name in component_styles)


def test_rich_theme_degrades_between_truecolor_ansi256_and_no_color() -> None:
    truecolor = _render("truecolor")
    ansi256 = _render("256")
    plain = _render("truecolor", no_color=True)

    assert "\x1b[38;2;127;167;201m" in truecolor
    assert "\x1b[38;5;" in ansi256
    assert "\x1b[48;" not in truecolor
    assert "\x1b[48;" not in ansi256
    assert "\x1b[" not in plain
    assert plain.strip() == "CapsLock ready"


def test_no_color_convention_and_prompt_style_use_only_terminal_default_background() -> (
    None
):
    assert no_color_enabled({"NO_COLOR": ""})
    assert not no_color_enabled({})

    colored = build_prompt_style(no_color=False)
    plain = build_prompt_style(no_color=True)
    assert all(
        "bg:" not in style or "bg:default" in style for _, style in colored.style_rules
    )
    assert all(
        "bg:" not in style or "noreverse" in style for _, style in colored.style_rules
    )
    assert all("#" not in style for _, style in plain.style_rules)


def test_markdown_inline_code_and_code_blocks_never_set_a_black_background() -> None:
    stream = StringIO()
    terminal = make_console(
        file=stream,
        color_system="truecolor",
        force_terminal=True,
        no_color=False,
        width=80,
    )
    terminal.print(
        assistant_content(
            "Use `capslock` here.\n\n```python\nprint('transparent')\n```"
        )
    )
    sgr_parameters = [
        [int(value) for value in match.split(";") if value]
        for match in re.findall(r"\x1b\[([0-9;]*)m", stream.getvalue())
    ]
    assert not any(
        48 in parameters
        or any(40 <= value <= 47 or 100 <= value <= 107 for value in parameters)
        for parameters in sgr_parameters
    )
