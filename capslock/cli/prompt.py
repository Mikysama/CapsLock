"""prompt-toolkit input, completion, highlighting, and key bindings."""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_bindings import merge_key_bindings
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu, MultiColumnCompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.shortcuts import choice
from prompt_toolkit.utils import get_cwidth

from ..domain import ActionRecord, ApprovalChoice, ApprovalDecision
from ..permissions import PermissionMode
from ..status import SPINNER_FRAMES
from ..theme import build_prompt_style
from .commands import command_descriptions, command_menu_completions
from .choices import ChoiceViewModel, questions_view_model
from .presentation import present_action, present_permission_request
from .keybindings import load_keybindings
from .suggestions import (
    StaticSuggestionProvider,
    UnifiedSuggestionProvider,
    WorkspaceFileSuggestionProvider,
)


class SlashCommandCompleter(Completer):
    def __init__(
        self,
        skill_provider: Callable[[], list[tuple[str, str]]] | None = None,
        *,
        workspace: Path | None = None,
    ) -> None:
        self.skill_provider = skill_provider or (lambda: [])
        providers = [StaticSuggestionProvider("$", "skill", self.skill_provider)]
        if workspace is not None:
            providers.append(WorkspaceFileSuggestionProvider(workspace))
        self.provider = UnifiedSuggestionProvider(providers)

    def get_completions(self, document: Document, complete_event: object):
        prefix = document.text_before_cursor
        token = prefix.rsplit(maxsplit=1)[-1] if prefix else ""
        if token.startswith(("$", "@")):
            for item in self.provider.suggestions(token):
                yield Completion(
                    item.value,
                    start_position=-len(token),
                    display=FormattedText([("class:command-name", item.value)]),
                    display_meta=item.description,
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


def select_choice(title: str, choices: Sequence[object]) -> str | None:
    """Render the shared selector with label/detail hierarchy and filtering."""

    model = ChoiceViewModel.from_choices(title, choices)
    options = model.options
    if model.filterable:
        from prompt_toolkit.shortcuts import prompt

        query = prompt(
            f"{title}\nFilter (blank shows all, Esc cancels): ",
            style=PROMPT_STYLE,
            key_bindings=_escape_bindings(),
        )
        if query is None:
            return None
        options = model.filtered(query)
        if not options:
            return None
    labels = [
        (
            item.value,
            FormattedText(
                [
                    ("class:user-input", item.label),
                    ("class:footer", f"  {item.detail}" if item.detail else ""),
                ]
            ),
        )
        for item in options
    ]
    default = (
        model.current if model.current in {item.value for item in options} else None
    )
    return choice(
        title,
        options=labels,
        default=default,
        style=PROMPT_STYLE,
        symbol="❯",
        bottom_toolbar="↑/↓ choose · Enter apply · Ctrl+C cancel",
        key_bindings=_escape_bindings(),
    )


def _escape_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def cancel(event: object) -> None:
        event.app.exit(result=None)

    return bindings


def answer_questions(raw_questions: Sequence[object]) -> dict[str, object] | None:
    """Collect validated ask_user answers with native prompt-toolkit controls."""

    from prompt_toolkit.shortcuts import checkboxlist_dialog, prompt

    questions = questions_view_model(raw_questions)
    if not questions:
        return None
    while True:
        answers: dict[str, object] = {}
        for index, question in enumerate(questions, 1):
            title = f"Question {index}/{len(questions)} · {question.question}"
            if question.multiple:
                values = [(item.value, item.label) for item in question.options]
                if question.allow_free_text:
                    values.append(("__other__", "Other…"))
                selected = checkboxlist_dialog(
                    title=title,
                    text="Select one or more options (Space toggles)",
                    values=values,
                    style=PROMPT_STYLE,
                    ok_text="Next",
                    cancel_text="Cancel",
                ).run()
                if not selected:
                    return None
                resolved = [str(value) for value in selected if value != "__other__"]
                if "__other__" in selected:
                    other = prompt("Other: ", style=PROMPT_STYLE).strip()
                    if not other:
                        return None
                    resolved.append(other)
                answers[question.identifier] = resolved
                continue

            options = list(question.options)
            if question.allow_free_text:
                from .choices import ChoiceOption

                options.append(ChoiceOption("__other__", "Other…", "Enter free text"))
            selected = choice(
                title,
                options=[
                    (
                        item.value,
                        FormattedText(
                            [
                                ("class:user-input", item.label),
                                (
                                    "class:footer",
                                    f"  {item.detail}" if item.detail else "",
                                ),
                            ]
                        ),
                    )
                    for item in options
                ],
                default=options[0].value if options else None,
                style=PROMPT_STYLE,
                symbol="❯",
                bottom_toolbar="↑/↓ choose · Enter next · Ctrl+C cancel",
                key_bindings=_escape_bindings(),
            )
            if selected == "__other__":
                selected = prompt("Other: ", style=PROMPT_STYLE).strip()
            if not selected:
                return None
            answers[question.identifier] = selected

        summary = "\n".join(
            f"{question.question}: "
            + (
                ", ".join(str(item) for item in answers[question.identifier])
                if isinstance(answers[question.identifier], list)
                else str(answers[question.identifier])
            )
            for question in questions
        )
        decision = choice(
            f"Review answers\n{summary}",
            options=(("submit", "Submit"), ("edit", "Edit answers")),
            default="submit",
            style=PROMPT_STYLE,
            symbol="❯",
            key_bindings=_escape_bindings(),
        )
        if decision == "submit":
            return answers


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
    usage: tuple[int | None, int | None, float | None] = (0, 0, 0.0),
    context: tuple[int | None, int] = (None, 0),
) -> FormattedText:
    terminal_width = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    queue_rows: list[tuple[str, str]] = []
    if queued_items:
        _identifier, latest = queued_items[-1]
        preview = " ".join(latest.split())
        queue_rows = [
            (
                "class:permission",
                _fit_cell(
                    f"Queued {len(queued_items)} · {preview} · ↑ edit",
                    max(20, terminal_width - 1),
                ).rstrip(),
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
        context=context,
    )
    footer = _combined_status(
        status_row, activity, spinner_frame, terminal_width=terminal_width
    )
    return FormattedText(
        [
            *queue_rows,
            *footer,
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
    usage: tuple[int | None, int | None, float | None] = (0, 0, 0.0),
    context: tuple[int | None, int] = (None, 0),
) -> FormattedText:
    terminal_width = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    status_row = _status_label(
        terminal_width,
        details_expanded=details_expanded,
        model=model,
        permission=permission,
        workspace=workspace,
        usage=usage,
        context=context,
    )
    bottom = "╰" + "─" * max(1, terminal_width - 2) + "╯"
    return FormattedText(
        [
            ("class:input-border", bottom),
            ("", "\n"),
            *_combined_status(
                status_row, activity, spinner_frame, terminal_width=terminal_width
            ),
        ]
    )


def _combined_status(
    status: str,
    activity: str | None,
    spinner_frame: int,
    *,
    terminal_width: int,
) -> list[tuple[str, str]]:
    if not activity:
        return [("class:footer", _fit_cell(status, terminal_width - 1).rstrip())]
    activity_label = activity if activity.endswith("...") else f"{activity}..."
    glyph = SPINNER_FRAMES[spinner_frame % len(SPINNER_FRAMES)]
    if terminal_width < 72:
        value = f"{glyph} {activity_label}"
    else:
        value = f"{glyph} {activity_label} · {status}"
    return [
        (
            "class:running class:running.bold",
            _fit_cell(value, terminal_width - 1).rstrip(),
        )
    ]


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
    usage: tuple[int | None, int | None, float | None],
    context: tuple[int | None, int],
) -> str:
    del details_expanded
    input_tokens, output_tokens, cost_usd = usage
    context_label = _context_label(*context)
    if any(value is None for value in usage) and terminal_width >= 72:
        return f"{model or '-'}  ·  {permission or '-'}  ·  {context_label}  ·  usage unknown"
    if terminal_width >= 100:
        return (
            f"{workspace or '-'}  ·  {model or '-'}  ·  {permission or '-'}  ·  "
            f"{context_label}  ·  {input_tokens}/{output_tokens} tok  ·  "
            f"${cost_usd:.4f}"
        )
    if terminal_width >= 72:
        return (
            f"{model or '-'}  ·  {permission or '-'}  ·  "
            f"{context_label}  ·  turn {input_tokens + output_tokens} tok"
        )
    return f"{permission or '-'}  ·  {context_label}"


def _context_label(used: int | None, limit: int) -> str:
    def compact(value: int) -> str:
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}m"
        if value >= 1_000:
            return f"{value / 1_000:.1f}k"
        return str(value)

    if used is None:
        return f"ctx —/{compact(limit)}"
    return f"ctx {compact(used)}/{compact(limit)} ({used * 100 / max(1, limit):.1f}%)"


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


def select_model(current: str, profiles: list[dict[str, object]] | None = None) -> str:
    available = [item for item in profiles or [] if item["available"]]
    unavailable = [
        f"{item['id']} ({item['provider']} / {item['model']}) unavailable"
        for item in profiles or []
        if not item["available"]
    ]
    if not available:
        raise ValueError(
            "No available model profiles; check credentials and capabilities. "
            + "; ".join(unavailable)
        )
    options = [
        (
            str(item["id"]),
            FormattedText(
                [
                    ("class:command-name", str(item["id"])),
                    ("class:footer", f"  {item['provider']} / {item['model']}"),
                ]
            ),
        )
        for item in available
    ]
    values = [str(item["id"]) for item in available]
    default = current if current in values else values[0]
    return choice(
        FormattedText(
            [
                ("class:command-name", "Select model\n"),
                *[("class:footer", item + "\n") for item in unavailable],
                ("class:footer", "↑/↓ choose · Enter apply"),
            ]
        ),
        options=options,
        default=default,
        style=PROMPT_STYLE,
        symbol="❯",
    )


def select_action_decision(
    action: ActionRecord,
) -> ApprovalChoice | ApprovalDecision:
    header = FormattedText(
        [
            ("class:command-name", "Allow CapsLock to execute this action?\n"),
            ("class:footer", "↑/↓ choose · Enter confirm"),
        ]
    )
    permission = action.request.get("_permission")
    suggestions = permission.get("suggestions") if isinstance(permission, dict) else []
    destinations = {
        item.get("destination") for item in suggestions if isinstance(item, dict)
    }
    if not destinations:
        options = [
            (ApprovalDecision.REJECT, "No, do not execute"),
            (ApprovalDecision.APPROVE, "Yes, execute"),
        ]
        default = ApprovalDecision.REJECT
    else:
        options = [
            (ApprovalChoice.REJECT, "No, do not execute"),
            (ApprovalChoice.APPROVE_ONCE, "Yes, execute once"),
        ]
        default = ApprovalChoice.REJECT
    if "session" in destinations:
        view = present_action(action)
        rule = next(
            (rule for target, rule in view.permission_rules if target == "session"),
            "allow matching action",
        )
        options.append((ApprovalChoice.APPROVE_SESSION, f"Session: {rule}"))
    if "local" in destinations:
        view = present_action(action)
        rule = next(
            (rule for target, rule in view.permission_rules if target == "local"),
            "allow matching action",
        )
        options.append((ApprovalChoice.APPROVE_LOCAL, f"Local: {rule}"))
    return choice(
        header,
        options=options,
        default=default,
        style=PROMPT_STYLE,
        symbol="❯",
    )


def select_permission_request_decision(
    request: dict[str, object],
) -> ApprovalChoice:
    suggestions = request.get("suggestions", [])
    destinations = {
        item.get("destination") for item in suggestions if isinstance(item, dict)
    }
    options = [
        (ApprovalChoice.REJECT, "No, reject this invocation"),
        (ApprovalChoice.APPROVE_ONCE, "Yes, allow this invocation once"),
    ]
    view = present_permission_request(request)
    if "session" in destinations:
        rule = next(
            (rule for target, rule in view.permission_rules if target == "session"),
            "allow matching tool",
        )
        options.append((ApprovalChoice.APPROVE_SESSION, f"Session: {rule}"))
    if "local" in destinations:
        rule = next(
            (rule for target, rule in view.permission_rules if target == "local"),
            "allow matching tool",
        )
        options.append((ApprovalChoice.APPROVE_LOCAL, f"Local: {rule}"))
    return choice(
        FormattedText(
            [
                ("class:command-name", "Allow this tool invocation?\n"),
                (
                    "class:footer",
                    f"{request.get('tool', 'tool')} · {request.get('reason', 'approval required')}\n"
                    f"{request.get('preview', '')}\n"
                    "↑/↓ choose · Enter confirm · default reject",
                ),
            ]
        ),
        options=options,
        default=ApprovalChoice.REJECT,
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
    buffer.cancel_completion()
    token = prefix.rsplit(maxsplit=1)[-1] if prefix else ""
    if prefix.startswith("/") or token.startswith(("$", "@")):
        buffer.start_completion(select_first=False)


def bind_slash_completion_refresh(buffer: object) -> None:
    """Refresh slash/Skill menus for every insertion, including fast paste."""

    def refresh(sender: object) -> None:
        refresh_slash_completion(sender)

    buffer.on_text_insert += refresh


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
    workspace: Path | None = None,
    keybinding_path: Path | None = None,
    recall_latest: Callable[[], Awaitable[str | None]] | None = None,
    show_help: Callable[[], Awaitable[None]] | None = None,
) -> PromptSession[str]:
    keymap = load_keybindings(keybinding_path)
    inline_bindings = KeyBindings()

    for key in keymap.bindings["insert_newline"]:
        inline_bindings.add(key)(lambda event: event.current_buffer.insert_text("\n"))

    for key in keymap.bindings["submit"]:
        inline_bindings.add(key)(
            lambda event: event.current_buffer.validate_and_handle()
        )

    def cancel(event: object) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    for key in keymap.bindings["cancel"]:
        inline_bindings.add(key, eager=True)(cancel)

    if toggle_details is not None:

        def _toggle_details(event: object) -> None:
            toggle_details()
            event.app.invalidate()

        for key in keymap.bindings["toggle_details"]:
            inline_bindings.add(key)(_toggle_details)

    if recall_latest is not None:

        def _recall_latest(event: object) -> None:
            buffer = event.current_buffer
            if buffer.text:
                buffer.history_backward()
                return

            async def recall() -> None:
                value = await recall_latest()
                if value is None:
                    return
                buffer.document = Document(value, cursor_position=len(value))
                event.app.invalidate()

            event.app.create_background_task(recall())

        inline_bindings.add("up", eager=True)(_recall_latest)

    if show_help is not None:

        def _show_help(event: object) -> None:
            if event.current_buffer.text:
                event.current_buffer.insert_text("?")
                return
            event.app.create_background_task(show_help())

        inline_bindings.add("?", eager=True)(_show_help)

    session = PromptSession(
        completer=SlashCommandCompleter(skill_provider, workspace=workspace),
        lexer=SlashCommandLexer(),
        key_bindings=merge_key_bindings([SLASH_KEY_BINDINGS, inline_bindings]),
        style=PROMPT_STYLE,
        enable_history_search=True,
        complete_while_typing=False,
        complete_style=CompleteStyle.COLUMN,
        reserve_space_for_menu=16,
        erase_when_done=True,
        include_default_pygments_style=False,
        show_frame=True,
        editing_mode=EditingMode.VI if keymap.vim_mode else EditingMode.EMACS,
    )
    bind_slash_completion_refresh(session.default_buffer)
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
