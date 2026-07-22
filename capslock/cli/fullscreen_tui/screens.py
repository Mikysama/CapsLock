"""Modal screens shared by the Textual fullscreen TUI."""

from __future__ import annotations

from collections.abc import Sequence

from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ...domain import ActionRecord, ApprovalDecision, SessionInfo
from ...permissions import PermissionMode
from .presentation import present_action


class ContentScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss_screen", "Close")]

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.dialog_title = title
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="content-dialog"):
            yield Static(self.dialog_title, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(self.content, markup=False)
            yield Static("Esc close", classes="input-guide")

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "reject", "Cancel"),
        Binding("n", "reject", "No"),
        Binding("y", "approve", "Yes"),
    ]

    def __init__(self, title: str, detail: str) -> None:
        super().__init__()
        self.dialog_title = title
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="confirm-dialog"):
            yield Static(self.dialog_title, classes="dialog-title")
            yield Static(self.detail, classes="dialog-detail")
            with Horizontal(classes="dialog-actions"):
                yield Button("No, cancel", id="reject", variant="default")
                yield Button("Yes, continue", id="approve", variant="warning")
            yield Static("Enter confirm · Esc cancel", classes="input-guide")

    def on_mount(self) -> None:
        self.query_one("#reject", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_reject(self) -> None:
        self.dismiss(False)

    def action_approve(self) -> None:
        self.dismiss(True)


class TextPromptScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, *, placeholder: str = "") -> None:
        super().__init__()
        self.dialog_title = title
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="confirm-dialog"):
            yield Static(self.dialog_title, classes="dialog-title")
            yield Input(placeholder=self.placeholder, id="prompt-value")
            yield Static("Enter submit · Esc cancel", classes="input-guide")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ApprovalScreen(ModalScreen[ApprovalDecision]):
    BINDINGS = [
        Binding("escape", "reject", "Reject"),
        Binding("n", "reject", "Reject"),
        Binding("y", "approve", "Approve"),
    ]

    def __init__(self, action: ActionRecord) -> None:
        super().__init__()
        self.action_record = action
        self.presentation = present_action(action)

    def compose(self) -> ComposeResult:
        view = self.presentation
        with Vertical(id="dialog", classes="approval-dialog"):
            yield Static("Allow CapsLock to execute this action?", classes="dialog-title permission-title")
            yield Static(Text.assemble((view.title, "bold"), (f"\n{view.subtitle}", "dim")))
            if view.target:
                yield Static(Text.assemble(("Target  ", "dim"), (view.target, "#89AFC8")))
            if view.preview:
                lexer = "diff" if view.preview_kind == "diff" else "bash" if view.preview_kind == "command" else "text"
                with VerticalScroll(classes="approval-preview"):
                    yield Static(Syntax(view.preview, lexer, word_wrap=True, theme="ansi_dark"))
            with Horizontal(classes="dialog-actions"):
                yield Button("No, reject", id="reject", variant="default")
                yield Button("Yes, execute", id="approve", variant="warning")
            yield Static("Default: reject · Enter confirm · Esc reject", classes="input-guide")

    def on_mount(self) -> None:
        self.query_one("#reject", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(
            ApprovalDecision.APPROVE
            if event.button.id == "approve"
            else ApprovalDecision.REJECT
        )

    def action_reject(self) -> None:
        self.dismiss(ApprovalDecision.REJECT)

    def action_approve(self) -> None:
        self.dismiss(ApprovalDecision.APPROVE)


class PermissionScreen(ModalScreen[PermissionMode | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: PermissionMode) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        options = [
            Option("Approve high-risk actions — files, commands and MCP ask first", id=PermissionMode.APPROVE_FOR_ME.value),
            Option("Approve every action — every proposal asks first", id=PermissionMode.ASK_FOR_APPROVAL.value),
            Option("Full access — safe actions run automatically", id=PermissionMode.FULL_ACCESS.value),
        ]
        with Vertical(id="dialog", classes="select-dialog"):
            yield Static("Select permission mode", classes="dialog-title permission-title")
            yield OptionList(*options, id="permission-options")
            yield Static("↑/↓ choose · Enter apply · Esc cancel", classes="input-guide")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        option_ids = [option.id for option in options.options]
        options.highlighted = option_ids.index(self.current.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(PermissionMode(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionPickerScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, sessions: Sequence[SessionInfo]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        options = [
            Option(
                Text.assemble(
                    (session.title, "bold"),
                    (f"\n{session.updated_at}  ·  {session.id[:12]}", "dim"),
                ),
                id=session.id,
            )
            for session in self.sessions
        ]
        with Vertical(id="dialog", classes="session-dialog"):
            yield Static("Resume a session", classes="dialog-title")
            yield OptionList(*options, id="session-options")
            yield Static("↑/↓ choose · Enter resume · Esc cancel", classes="input-guide")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HistorySearchScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, history: Sequence[str]) -> None:
        super().__init__()
        self.history = tuple(dict.fromkeys(reversed(history)))

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="select-dialog"):
            yield Static("Search input history", classes="dialog-title")
            yield Input(placeholder="Type to filter…", id="history-query")
            yield OptionList(id="history-options")
            yield Static("Enter restore · Esc cancel", classes="input-guide")

    def on_mount(self) -> None:
        self._update_options("")
        self.query_one(Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._update_options(event.value)

    def _update_options(self, query: str) -> None:
        value = query.casefold()
        matches = [item for item in self.history if value in item.casefold()][:50]
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(Option(item, id=str(index)) for index, item in enumerate(matches))
        options._capslock_matches = matches

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        matches = getattr(event.option_list, "_capslock_matches", [])
        if event.option.id is not None:
            self.dismiss(matches[int(event.option.id)])

    def action_cancel(self) -> None:
        self.dismiss(None)
