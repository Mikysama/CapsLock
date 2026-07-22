"""prompt-toolkit input, completion, highlighting, and key bindings."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_bindings import merge_key_bindings
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu, MultiColumnCompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.shortcuts import choice
from prompt_toolkit.utils import get_cwidth

from ..domain import ActionRecord, ApprovalDecision
from ..permissions import PermissionMode
from ..status import SPINNER_FRAMES
from ..theme import build_prompt_style
from .commands import command_descriptions, command_menu_completions


class SlashCommandCompleter(Completer):
    def __init__(
        self, skill_provider: Callable[[], list[tuple[str, str]]] | None = None
    ) -> None:
        self.skill_provider = skill_provider or (lambda: [])

    def get_completions(self, document: Document, complete_event: object):
        prefix = document.text_before_cursor
        if prefix.startswith("$"):
            for name, description in self.skill_provider():
                command = f"${name}"
                if command.casefold().startswith(prefix.casefold()):
                    yield Completion(
                        command,
                        start_position=-len(prefix),
                        display=FormattedText([("class:command-name", command)]),
                        display_meta=description,
                    )
            return
        if not prefix.startswith("/"):
            return
        descriptions = command_descriptions()
        for command in command_menu_completions(prefix):
            insertion = (
                f"{command} " if prefix.casefold() == command.casefold() else command
            )
            yield Completion(
                insertion,
                start_position=-len(prefix),
                display=FormattedText([("class:command-name", command)]),
                display_meta=descriptions[command],
            )


class SlashCommandLexer(Lexer):
    def lex_document(self, document: Document):
        def get_line(line_number: int):
            line = document.lines[line_number]
            return (
                [("class:slash-command", line)]
                if line.startswith("/")
                else [("class:user-input", line)]
            )

        return get_line


PROMPT_STYLE = build_prompt_style()


def prompt_tokens(
    mode: PermissionMode,
    width: int | None = None,
) -> FormattedText:
    return FormattedText([("class:prompt", "❯ ")])


def prompt_prelude(
    width: int | None = None,
    *,
    queued_items: tuple[tuple[str, str], ...] = (),
    activity: str | None = None,
    spinner_frame: int = 0,
    details_expanded: bool = False,
    model: str | None = None,
    permission: str | None = None,
    workspace: str | None = None,
    usage: tuple[int, int, float] = (0, 0, 0.0),
) -> FormattedText:
    terminal_width = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    queue_rows: list[tuple[str, str]] = []
    if queued_items:
        previews = "  ·  ".join(
            f"{identifier[:8]} {text}" for identifier, text in queued_items[:3]
        )
        queue_rows = [
            (
                "class:permission",
                _fit_cell(f"Queue  {previews}", max(20, terminal_width - 1)).rstrip(),
            ),
            ("", "\n"),
        ]
    status_row = _status_label(
        terminal_width,
        details_expanded=details_expanded,
        model=model,
        permission=permission,
        workspace=workspace,
        usage=usage,
    )
    activity_rows = _activity_fragments(activity, spinner_frame)
    return FormattedText(
        [
            *queue_rows,
            *activity_rows,
            ("class:footer", _fit_cell(status_row, terminal_width - 1).rstrip()),
        ]
    )


def permission_rprompt(mode: PermissionMode) -> FormattedText:
    return FormattedText(
        [
            ("class:permission", f"{mode.value} "),
            ("class:input-border", "│"),
        ]
    )


def prompt_footer(
    width: int | None = None,
    *,
    activity: str | None = None,
    spinner_frame: int = 0,
    details_expanded: bool = False,
    model: str | None = None,
    permission: str | None = None,
    workspace: str | None = None,
    usage: tuple[int, int, float] = (0, 0, 0.0),
) -> FormattedText:
    terminal_width = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    status = _activity_fragments(activity, spinner_frame, trailing_newline=False)
    status_row = _status_label(
        terminal_width,
        details_expanded=details_expanded,
        model=model,
        permission=permission,
        workspace=workspace,
        usage=usage,
    )
    bottom = "╰" + "─" * max(1, terminal_width - 2) + "╯"
    return FormattedText(
        [
            ("class:input-border", bottom),
            ("", "\n"),
            *(status or [("", " ")]),
            ("", "\n"),
            ("class:footer", _fit_cell(status_row, terminal_width - 1).rstrip()),
        ]
    )


def _activity_fragments(
    activity: str | None,
    spinner_frame: int,
    *,
    trailing_newline: bool = True,
) -> list[tuple[str, str]]:
    if not activity:
        return []
    activity_label = activity if activity.endswith("...") else f"{activity}..."
    activity_style = (
        "class:thinking"
        if activity.casefold().startswith("thinking")
        else "class:running"
    )
    fragments = [
        (
            "class:running class:running.bold",
            f"{SPINNER_FRAMES[spinner_frame % len(SPINNER_FRAMES)]} ",
        ),
        (f"{activity_style} {activity_style}.bold", activity_label),
    ]
    if trailing_newline:
        fragments.append(("", "\n"))
    return fragments


def _status_label(
    terminal_width: int,
    *,
    details_expanded: bool,
    model: str | None,
    permission: str | None,
    workspace: str | None,
    usage: tuple[int, int, float],
) -> str:
    detail_state = "details on" if details_expanded else "details off"
    input_tokens, output_tokens, cost_usd = usage
    if terminal_width >= 100:
        return (
            f"{workspace or '-'}  ·  {model or '-'}  ·  {permission or '-'}  ·  "
            f"{input_tokens}/{output_tokens} tok  ·  ${cost_usd:.4f}  ·  {detail_state}"
        )
    if terminal_width >= 72:
        return (
            f"{model or '-'}  ·  {permission or '-'}  ·  "
            f"{input_tokens}/{output_tokens} tok  ·  {detail_state}"
        )
    return (
        f"{permission or '-'}  ·  {input_tokens + output_tokens} tok  ·  "
        f"{'details on' if details_expanded else 'details off'}"
    )


def select_session(
    sessions: list[object],
    width: int | None = None,
    *,
    title: str = "Resume a session",
) -> str:
    if not sessions:
        raise ValueError("no saved sessions are available")
    terminal_width = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    number_width = max(2, len(str(len(sessions))))
    title_width, updated_width, session_width = _session_column_widths(
        terminal_width,
        number_width,
    )
    header_indent = " " * (6 + number_width)
    header = FormattedText(
        [
            ("class:command-name", f"{title}\n"),
            ("", header_indent),
            ("class:footer", _fit_cell("Title", title_width)),
            ("", "  "),
            ("class:footer", _fit_cell("Updated (UTC)", updated_width)),
            ("", "  "),
            ("class:footer", _fit_cell("Session ID", session_width)),
        ]
    )
    options = [
        (
            session.id,
            FormattedText(
                [
                    ("", " " * (number_width - max(2, len(str(index))))),
                    ("class:user-input", _fit_cell(session.title, title_width)),
                    ("", "  "),
                    (
                        "class:footer",
                        _fit_cell(_updated_at(session.updated_at), updated_width),
                    ),
                    ("", "  "),
                    ("class:command-name", _fit_cell(session.id, session_width)),
                ]
            ),
        )
        for index, session in enumerate(sessions, start=1)
    ]
    return choice(
        header,
        options=options,
        default=sessions[0].id,
        style=PROMPT_STYLE,
        symbol="❯",
    )


def select_permission_mode(current: PermissionMode) -> PermissionMode:
    labels = {
        PermissionMode.APPROVE_FOR_ME: (
            "Approve high-risk actions",
            "Files, commands, and MCP wait for approval; Web remains audited.",
        ),
        PermissionMode.ASK_FOR_APPROVAL: (
            "Approve every action",
            "Every proposal waits for an explicit decision.",
        ),
        PermissionMode.FULL_ACCESS: (
            "Full access",
            "Safe actions run automatically; Skill file changes still wait.",
        ),
    }
    options = [
        (
            mode,
            FormattedText(
                [
                    ("class:command-name", title),
                    ("class:footer", f"  {description}"),
                ]
            ),
        )
        for mode, (title, description) in labels.items()
    ]
    return choice(
        FormattedText(
            [
                ("class:command-name", "Select permission mode\n"),
                ("class:footer", "↑/↓ choose · Enter apply"),
            ]
        ),
        options=options,
        default=current,
        style=PROMPT_STYLE,
        symbol="❯",
    )


def select_action_decision(_action: ActionRecord) -> ApprovalDecision:
    header = FormattedText(
        [
            ("class:command-name", "Allow CapsLock to execute this action?\n"),
            ("class:footer", "↑/↓ choose · Enter confirm"),
        ]
    )
    return choice(
        header,
        options=[
            (ApprovalDecision.REJECT, "No, do not execute"),
            (ApprovalDecision.APPROVE, "Yes, execute"),
        ],
        default=ApprovalDecision.REJECT,
        style=PROMPT_STYLE,
        symbol="❯",
    )


def _session_column_widths(
    terminal_width: int, number_width: int
) -> tuple[int, int, int]:
    fixed_width = 12 + number_width
    updated_width = 16
    session_width = (
        32 if terminal_width >= fixed_width + updated_width + 32 + 12 else 12
    )
    title_width = max(
        8, min(50, terminal_width - updated_width - session_width - fixed_width)
    )
    return title_width, updated_width, session_width


def _fit_cell(value: str, width: int) -> str:
    text = " ".join(str(value).split())
    if get_cwidth(text) > width:
        text = text.rstrip()
        while text and get_cwidth(text) > width - 3:
            text = text[:-1].rstrip()
        text += "..."
    return text + " " * max(0, width - get_cwidth(text))


def _updated_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


def refresh_slash_completion(buffer: object) -> None:
    prefix = buffer.document.text_before_cursor
    if prefix.startswith(("/", "$")):
        buffer.start_completion(select_first=False)
    else:
        buffer.cancel_completion()


SLASH_KEY_BINDINGS = KeyBindings()


@SLASH_KEY_BINDINGS.add("backspace")
def _backspace_and_refresh(event: object) -> None:
    event.current_buffer.delete_before_cursor()
    refresh_slash_completion(event.current_buffer)


@SLASH_KEY_BINDINGS.add("delete")
def _delete_and_refresh(event: object) -> None:
    event.current_buffer.delete()
    refresh_slash_completion(event.current_buffer)


def anchor_completion_menus(container: object) -> None:
    seen: set[int] = set()

    def visit(node: object) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        for floating in getattr(node, "floats", ()):
            if isinstance(
                floating.content, (CompletionsMenu, MultiColumnCompletionsMenu)
            ):
                floating.xcursor = False
                floating.left = 0
        get_children = getattr(node, "get_children", None)
        if get_children is not None:
            for child in get_children():
                visit(child)

    visit(container)


def prompt_session(
    skill_provider: Callable[[], list[tuple[str, str]]] | None = None,
    *,
    toggle_details: Callable[[], None] | None = None,
    prelude_provider: Callable[[], FormattedText] | None = None,
) -> PromptSession[str]:
    inline_bindings = KeyBindings()

    @inline_bindings.add("c-j")
    def _insert_newline(event: object) -> None:
        event.current_buffer.insert_text("\n")

    @inline_bindings.add("c-m")
    def _submit(event: object) -> None:
        event.current_buffer.validate_and_handle()

    @inline_bindings.add("c-c", eager=True)
    def _cancel_or_exit(event: object) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    if toggle_details is not None:

        @inline_bindings.add("c-o")
        def _toggle_details(event: object) -> None:
            toggle_details()
            event.app.invalidate()

    session = PromptSession(
        completer=SlashCommandCompleter(skill_provider),
        lexer=SlashCommandLexer(),
        key_bindings=merge_key_bindings([SLASH_KEY_BINDINGS, inline_bindings]),
        style=PROMPT_STYLE,
        enable_history_search=True,
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        reserve_space_for_menu=16,
        erase_when_done=True,
        include_default_pygments_style=False,
        show_frame=True,
    )
    root = session.app.layout.container
    if isinstance(root, HSplit) and root.children:
        framed = root.children[0]
        main_input = getattr(framed, "alternative_content", None)
        if main_input is not None:
            framed.content = _composer_frame(main_input)
        if prelude_provider is not None:
            root.children.insert(
                0,
                Window(
                    FormattedTextControl(prelude_provider),
                    dont_extend_height=True,
                ),
            )
    anchor_completion_menus(session.app.layout.container)
    return session


def _composer_frame(main_input: object) -> HSplit:
    border_style = "class:frame.border"

    def label(value: str, width: int) -> Window:
        return Window(
            FormattedTextControl([(border_style, value)]),
            width=width,
            height=1,
            dont_extend_width=True,
        )

    top = VSplit(
        [
            label("╭─ Ask CapsLock ", len("╭─ Ask CapsLock ")),
            Window(char="─", style=border_style, height=1),
            label("╮", 1),
        ],
        height=1,
    )
    body = VSplit(
        [
            label("│ ", 2),
            main_input,
            label("│", 1),
        ]
    )
    bottom = VSplit(
        [
            label("╰", 1),
            Window(char="─", style=border_style, height=1),
            label("╯", 1),
        ],
        height=1,
    )
    return HSplit([top, body, bottom])


def move_selection(selected: int, key: str, option_count: int) -> int:
    if key.casefold() in {"up", "left", "k"}:
        return (selected - 1) % option_count
    if key.casefold() in {"down", "right", "j"}:
        return (selected + 1) % option_count
    return selected


_prompt_tokens = prompt_tokens
_permission_rprompt = permission_rprompt
_refresh_slash_completion = refresh_slash_completion
_anchor_completion_menus = anchor_completion_menus
_move_selection = move_selection
