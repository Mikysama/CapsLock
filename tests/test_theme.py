from io import StringIO
import re

from capslock.cli.views.conversation import assistant_content, user_message
from capslock.theme import (
    RICH_BACKGROUND_STYLE_DEFINITIONS,
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
    assert THEME_TOKENS["userPromptBackground"] == "#E0E0E0"
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
    assert RICH_BACKGROUND_STYLE_DEFINITIONS["user.background"] == "#E0E0E0"
    assert RICH_THEME.styles["user.background"].bgcolor is not None
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


def test_inline_user_prompt_background_fills_row_next_to_blue_marker() -> None:
    stream = StringIO()
    terminal = make_console(
        file=stream,
        color_system="truecolor",
        force_terminal=True,
        no_color=False,
        width=20,
    )
    lines = terminal.render_lines(user_message("hello"), terminal.options, pad=False)

    assert len(lines) == 1
    assert lines[0][0].text == "▌"
    assert lines[0][0].style is not None
    assert lines[0][0].style.bgcolor is None
    row = "".join(segment.text for segment in lines[0])
    assert row.startswith("▌  ❯ hello")
    assert len(row) == terminal.options.max_width
    assert all(
        segment.style is not None and segment.style.bgcolor is not None
        for segment in lines[0][1:]
    )
