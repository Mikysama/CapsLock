"""Modal screens shared by the Textual fullscreen TUI."""

from __future__ import annotations

from collections.abc import Sequence

from rich.syntax import Syntax
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from ...domain import ActionRecord, ApprovalChoice, ApprovalDecision, SessionInfo
from ...models import SELECTABLE_MODELS
from ...permissions import PermissionMode
from ...theme import terminal_style
from ..choices import ChoiceViewModel, questions_view_model
from ..command_ui import PlanApprovalResult
from ..presentation import ToolPresentation, present_action, present_permission_request
from .rendering import TransparentBackground
from ..views.conversation import tool_group


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


class MarkdownContentScreen(ContentScreen):
    """Scrollable content dialog rendered with the same Markdown engine as chat."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="content-dialog"):
            yield Static(self.dialog_title, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(
                    TransparentBackground(
                        RichMarkdown(
                            self.content,
                            code_theme="ansi_dark",
                            hyperlinks=True,
                        )
                    )
                )
            yield Static("Esc close", classes="input-guide")


class SideQuestionScreen(ModalScreen[None]):
    """Isolated side-agent result using the normal assistant Markdown renderer."""

    BINDINGS = [Binding("escape", "dismiss_screen", "Close")]

    def __init__(
        self,
        question: str,
        content: str,
        tools: Sequence[ToolPresentation] = (),
    ) -> None:
        super().__init__()
        self.question = question
        self.content = content
        self.tools = tuple(tools)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="content-dialog"):
            yield Static(
                Text.assemble(
                    ("/btw ", terminal_style("warning", "bold")),
                    (self.question, "dim"),
                ),
                classes="dialog-title",
            )
            with VerticalScroll(classes="dialog-scroll"):
                if self.tools:
                    yield Static(tool_group(list(self.tools), expanded=True))
                yield Static(
                    TransparentBackground(
                        RichMarkdown(
                            self.content,
                            code_theme="ansi_dark",
                            hyperlinks=True,
                        )
                    ),
                    classes="message assistant",
                )
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


class EnterPlanModeScreen(ModalScreen[bool]):
    """Claude Code-compatible explanation shown for model-proposed planning."""

    BINDINGS = [Binding("escape", "reject", "Do not enter Plan Mode")]

    def __init__(self, objective: str) -> None:
        super().__init__()
        self.objective = objective

    def compose(self) -> ComposeResult:
        options = OptionList(
            Option("Yes, enter Plan Mode", id="enter"),
            Option("No, start implementing now", id="reject"),
            id="plan-entry-options",
        )
        with Vertical(id="dialog", classes="plan-entry-dialog"):
            yield Static("Enter Plan Mode?", classes="dialog-title plan-title")
            yield Static(
                "CapsLock wants to enter Plan Mode to explore and design an "
                "implementation approach."
            )
            yield Static(
                Text.assemble(("Objective  ", "dim"), (self.objective, "bold"))
            )
            yield Static(
                "In Plan Mode, CapsLock will:\n"
                "  - Explore the codebase\n"
                "  - Identify existing patterns\n"
                "  - Design an implementation strategy\n"
                "  - Present a plan for your approval",
                classes="dialog-detail",
            )
            yield Static(
                "No code changes will be made until you approve the plan.",
                classes="dialog-detail",
            )
            yield options
            yield Static(
                "↑/↓ choose · Enter confirm · Esc do not enter",
                classes="input-guide",
            )

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id == "enter")

    def action_reject(self) -> None:
        self.dismiss(False)


class PlanApprovalScreen(ModalScreen[PlanApprovalResult]):
    """Review the complete plan and either implement or continue planning."""

    BINDINGS = [Binding("escape", "keep_planning", "Keep planning")]

    def __init__(
        self,
        *,
        objective: str,
        content: str,
        revision: int,
        sha256: str,
        permission_mode: str,
    ) -> None:
        super().__init__()
        self.objective = objective
        self.content = content
        self.revision = revision
        self.sha256 = sha256
        self.permission_mode = permission_mode

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="plan-approval-dialog"):
            yield Static("Ready to code?", classes="dialog-title plan-title")
            yield Static("Here is CapsLock's plan:")
            yield Static(
                Text.assemble(
                    (self.objective, "bold"),
                    (
                        f"\nrevision {self.revision}  ·  sha256 {self.sha256[:12]}",
                        "dim",
                    ),
                )
            )
            with VerticalScroll(classes="plan-preview"):
                yield Static(
                    TransparentBackground(
                        RichMarkdown(
                            self.content,
                            code_theme="ansi_dark",
                            hyperlinks=True,
                        )
                    )
                )
            yield Static(
                "Plan approval starts a new implementation run. Tool calls still "
                f"follow {self.permission_mode} permissions.",
                classes="dialog-detail",
            )
            yield OptionList(
                Option(
                    Text.assemble(
                        ("Yes, start implementation", "bold"),
                        (f"\nusing {self.permission_mode}", "dim"),
                    ),
                    id="implement",
                ),
                Option(
                    Text.assemble(
                        ("No, keep planning", "bold"),
                        ("\ntell CapsLock what to change", "dim"),
                    ),
                    id="feedback",
                ),
                Option("Reject and exit Plan Mode", id="reject"),
                id="plan-approval-options",
            )
            yield Input(
                placeholder="Tell CapsLock what to change (optional)",
                id="plan-feedback",
            )
            yield Static(
                "↑/↓ choose · Enter confirm · Esc keep planning",
                classes="input-guide",
            )

    def on_mount(self) -> None:
        self.query_one("#plan-approval-options", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        choice = str(event.option.id)
        if choice == "feedback":
            self.query_one("#plan-feedback", Input).focus()
            return
        self.dismiss(PlanApprovalResult(choice))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(PlanApprovalResult("feedback", value or None))

    def action_keep_planning(self) -> None:
        self.dismiss(PlanApprovalResult("feedback"))


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


class InputRequestScreen(ModalScreen[dict[str, object] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, questions: list[dict[str, object]]) -> None:
        super().__init__()
        self.questions = questions_view_model(questions)
        self.step = 0
        self.answers: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="content-dialog"):
            yield Static("CapsLock needs your input", classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                for index, question in enumerate(self.questions):
                    with Vertical(id=f"question-step-{index}", classes="question-step"):
                        yield Static(
                            f"Question {index + 1}/{len(self.questions)}\n{question.question}",
                            classes="question-title",
                            markup=False,
                        )
                        if question.multiple:
                            values = [
                                (item.label, item.value, False)
                                for item in question.options
                            ]
                            if question.allow_free_text:
                                values.append(("Other…", "__other__", False))
                            yield SelectionList(*values, id=f"answer-options-{index}")
                        else:
                            options = [
                                Option(
                                    Text.assemble(
                                        (item.label, ""),
                                        (f"\n{item.detail}", "dim")
                                        if item.detail
                                        else ("", ""),
                                    ),
                                    id=item.value,
                                )
                                for item in question.options
                            ]
                            if question.allow_free_text:
                                options.append(Option("Other…", id="__other__"))
                            yield OptionList(*options, id=f"answer-options-{index}")
                        if question.allow_free_text:
                            yield Input(
                                placeholder="Other answer",
                                id=f"answer-other-{index}",
                            )
                        yield Static(
                            "Choose an answer before continuing",
                            id=f"answer-error-{index}",
                            classes="question-error",
                        )
                with Vertical(id="answer-summary"):
                    yield Static("Review answers", classes="question-title")
                    yield Static("", id="answer-summary-content", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Back", id="back")
                yield Button("Next", id="next", variant="primary")
                yield Button("Submit", id="submit", variant="primary")
            yield Static(
                "↑/↓ choose · Space toggle · Enter next · Esc cancel",
                classes="input-guide",
            )

    def on_mount(self) -> None:
        if not self.questions:
            self.dismiss(None)
            return
        self._show_step(0)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "back":
            if self.step >= len(self.questions):
                self._show_step(len(self.questions) - 1)
            elif self.step > 0:
                self._show_step(self.step - 1)
            return
        if event.button.id == "next":
            if not self._save_step(self.step):
                return
            if self.step + 1 < len(self.questions):
                self._show_step(self.step + 1)
            else:
                self._show_summary()
            return
        if event.button.id == "submit":
            self.dismiss(dict(self.answers))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == f"answer-options-{self.step}":
            self.answers[self.questions[self.step].identifier] = str(event.option.id)

    def _save_step(self, index: int) -> bool:
        question = self.questions[index]
        other = (
            self.query_one(f"#answer-other-{index}", Input).value.strip()
            if question.allow_free_text
            else ""
        )
        if question.multiple:
            selected = list(
                self.query_one(f"#answer-options-{index}", SelectionList).selected
            )
            if "__other__" in selected:
                selected.remove("__other__")
                if other:
                    selected.append(other)
                else:
                    return self._invalid(index)
            if not selected and other:
                selected.append(other)
            if not selected:
                return self._invalid(index)
            self.answers[question.identifier] = [str(value) for value in selected]
        else:
            value = self.answers.get(question.identifier)
            if value == "__other__":
                value = other
            elif value is None and other:
                value = other
            if not value:
                return self._invalid(index)
            self.answers[question.identifier] = str(value)
        self.query_one(f"#answer-error-{index}").display = False
        return True

    def _invalid(self, index: int) -> bool:
        error = self.query_one(f"#answer-error-{index}")
        error.display = True
        error.add_class("invalid")
        return False

    def _show_step(self, index: int) -> None:
        self.step = index
        for item_index in range(len(self.questions)):
            self.query_one(f"#question-step-{item_index}").display = item_index == index
            self.query_one(f"#answer-error-{item_index}").display = False
        self.query_one("#answer-summary").display = False
        self.query_one("#back", Button).display = index > 0
        self.query_one("#next", Button).display = True
        self.query_one("#submit", Button).display = False
        self.query_one(f"#answer-options-{index}").focus()

    def _show_summary(self) -> None:
        self.step = len(self.questions)
        for index in range(len(self.questions)):
            self.query_one(f"#question-step-{index}").display = False
        rows = []
        for question in self.questions:
            answer = self.answers[question.identifier]
            value = ", ".join(answer) if isinstance(answer, list) else str(answer)
            rows.append(f"{question.question}\n  {value}")
        self.query_one("#answer-summary-content", Static).update("\n\n".join(rows))
        self.query_one("#answer-summary").display = True
        self.query_one("#back", Button).display = True
        self.query_one("#next", Button).display = False
        self.query_one("#submit", Button).display = True
        self.query_one("#submit", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceScreen(ModalScreen[str | None]):
    """Reusable, optionally filterable selector for slash-command workflows."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, model: ChoiceViewModel) -> None:
        super().__init__()
        self.model = model

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="select-dialog"):
            yield Static(self.model.title, classes="dialog-title")
            if self.model.filterable:
                yield Input(placeholder="Filter options", id="choice-filter")
            yield OptionList(id="choice-options")
            yield Static(
                "Type to filter · ↑/↓ choose · Enter apply · Esc cancel",
                classes="input-guide",
            )

    def on_mount(self) -> None:
        self._replace_options(self.model.options)
        target = (
            self.query_one("#choice-filter", Input)
            if self.model.filterable
            else self.query_one(OptionList)
        )
        target.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "choice-filter":
            self._replace_options(self.model.filtered(event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "choice-filter":
            return
        options = self.model.filtered(event.value)
        if len(options) == 1:
            self.dismiss(options[0].value)
        else:
            self.query_one(OptionList).focus()

    def on_key(self, event) -> None:
        if (
            event.key in {"up", "down"}
            and self.model.filterable
            and self.query_one("#choice-filter", Input).has_focus
        ):
            event.prevent_default()
            event.stop()
            options = self.query_one(OptionList)
            options.focus()
            options.action_cursor_up() if event.key == "up" else options.action_cursor_down()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss(str(event.option.id))

    def _replace_options(self, values) -> None:
        options = self.query_one("#choice-options", OptionList)
        options.clear_options()
        options.add_options(
            [
                Option(
                    Text.assemble(
                        (item.label, ""),
                        (f"\n{item.detail}", "dim") if item.detail else ("", ""),
                    ),
                    id=item.value,
                )
                for item in values
            ]
        )
        current = next(
            (
                index
                for index, item in enumerate(values)
                if item.value == self.model.current
            ),
            0,
        )
        options.highlighted = current if values else None

    def action_cancel(self) -> None:
        self.dismiss(None)


class ApprovalScreen(ModalScreen[ApprovalChoice | ApprovalDecision]):
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
            yield Static(
                "Allow CapsLock to execute this action?",
                classes="dialog-title permission-title",
            )
            yield Static(
                Text.assemble((view.title, "bold"), (f"\n{view.subtitle}", "dim"))
            )
            if view.target:
                yield Static(
                    Text.assemble(
                        ("Target  ", "dim"), (view.target, terminal_style("path"))
                    )
                )
            for label, detail in view.metadata:
                yield Static(Text.assemble((f"{label}  ", "dim"), (detail, "")))
            if view.risk_reason:
                yield Static(
                    Text.assemble(
                        ("Risk  ", "dim"),
                        (view.risk_reason, terminal_style("warning")),
                    )
                )
            if view.rollback:
                yield Static(Text.assemble(("Rollback  ", "dim"), (view.rollback, "")))
            if view.preview:
                lexer = (
                    "diff"
                    if view.preview_kind == "diff"
                    else "bash"
                    if view.preview_kind == "command"
                    else "text"
                )
                with VerticalScroll(classes="approval-preview"):
                    yield Static(
                        TransparentBackground(
                            Syntax(
                                view.preview,
                                lexer,
                                word_wrap=True,
                                theme="ansi_dark",
                            )
                        )
                    )
            with Horizontal(classes="dialog-actions"):
                yield Button("No, reject", id="reject", variant="default")
                yield Button("Yes, once", id="approve", variant="warning")
                destinations = self._suggestion_destinations()
                if "session" in destinations:
                    yield Button(
                        f"Session: {self._suggestion_rule('session')}",
                        id="approve_session",
                    )
                if "local" in destinations:
                    yield Button(
                        f"Local: {self._suggestion_rule('local')}",
                        id="approve_local",
                    )
            yield Static(
                "Default: reject · Enter confirm · Esc reject", classes="input-guide"
            )

    def on_mount(self) -> None:
        self.query_one("#reject", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not self._suggestion_destinations():
            self.dismiss(
                ApprovalDecision.APPROVE
                if event.button.id == "approve"
                else ApprovalDecision.REJECT
            )
            return
        choices = {
            "approve": ApprovalChoice.APPROVE_ONCE,
            "approve_session": ApprovalChoice.APPROVE_SESSION,
            "approve_local": ApprovalChoice.APPROVE_LOCAL,
            "reject": ApprovalChoice.REJECT,
        }
        self.dismiss(choices.get(str(event.button.id), ApprovalChoice.REJECT))

    def action_reject(self) -> None:
        self.dismiss(
            ApprovalChoice.REJECT
            if self._suggestion_destinations()
            else ApprovalDecision.REJECT
        )

    def action_approve(self) -> None:
        self.dismiss(
            ApprovalChoice.APPROVE_ONCE
            if self._suggestion_destinations()
            else ApprovalDecision.APPROVE
        )

    def _suggestion_destinations(self) -> set[str]:
        permission = self.action_record.request.get("_permission")
        suggestions = (
            permission.get("suggestions") if isinstance(permission, dict) else []
        )
        return {
            str(item.get("destination"))
            for item in suggestions
            if isinstance(item, dict)
        }

    def _suggestion_rule(self, destination: str) -> str:
        return next(
            (
                rule
                for target, rule in self.presentation.permission_rules
                if target == destination
            ),
            "allow matching action",
        )


class PermissionApprovalScreen(ModalScreen[ApprovalChoice]):
    """Fail-safe approval for a durable non-Action tool invocation."""

    BINDINGS = [
        Binding("escape", "reject", "Reject"),
        Binding("n", "reject", "Reject"),
        Binding("y", "approve", "Approve once"),
    ]

    def __init__(self, request: dict[str, object]) -> None:
        super().__init__()
        self.request = request
        self.presentation = present_permission_request(request)

    def compose(self) -> ComposeResult:
        destinations = {item[0] for item in self.presentation.permission_rules}
        view = self.presentation
        with Vertical(id="dialog", classes="approval-dialog"):
            yield Static(
                "Allow CapsLock to invoke this tool?",
                classes="dialog-title permission-title",
            )
            yield Static(
                Text.assemble(
                    (view.title, "bold"),
                    (f"\n{view.risk_reason or 'Approval required'}", "dim"),
                )
            )
            for label, detail in view.metadata:
                yield Static(Text.assemble((f"{label}  ", "dim"), (detail, "")))
            if view.preview:
                with VerticalScroll(classes="approval-preview"):
                    yield Static(view.preview, markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("No, reject", id="reject", variant="default")
                yield Button("Yes, once", id="approve_once", variant="warning")
                if "session" in destinations:
                    yield Button(
                        f"Session: {self._rule('session')}", id="approve_session"
                    )
                if "local" in destinations:
                    yield Button(f"Local: {self._rule('local')}", id="approve_local")
            yield Static(
                "Default: reject · Enter confirm · Esc reject", classes="input-guide"
            )

    def on_mount(self) -> None:
        self.query_one("#reject", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {
            "approve_once": ApprovalChoice.APPROVE_ONCE,
            "approve_session": ApprovalChoice.APPROVE_SESSION,
            "approve_local": ApprovalChoice.APPROVE_LOCAL,
            "reject": ApprovalChoice.REJECT,
        }
        self.dismiss(choices.get(str(event.button.id), ApprovalChoice.REJECT))

    def action_reject(self) -> None:
        self.dismiss(ApprovalChoice.REJECT)

    def action_approve(self) -> None:
        self.dismiss(ApprovalChoice.APPROVE_ONCE)

    def _rule(self, destination: str) -> str:
        return next(
            (
                rule
                for target, rule in self.presentation.permission_rules
                if target == destination
            ),
            "allow matching tool",
        )


class PermissionScreen(ModalScreen[PermissionMode | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: PermissionMode) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        options = [
            Option(
                "Approve high-risk actions — files, commands and MCP ask first",
                id=PermissionMode.APPROVE_FOR_ME.value,
            ),
            Option(
                "Approve every action — every proposal asks first",
                id=PermissionMode.ASK_FOR_APPROVAL.value,
            ),
            Option(
                "Full access — safe actions run automatically",
                id=PermissionMode.FULL_ACCESS.value,
            ),
        ]
        with Vertical(id="dialog", classes="select-dialog"):
            yield Static(
                "Select permission mode", classes="dialog-title permission-title"
            )
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


class ModelScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: str) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        descriptions = {
            "deepseek-v4-flash": "Fast model for everyday tasks",
            "deepseek-v4-pro": "More capable model for complex tasks",
        }
        options = [
            Option(
                Text.assemble(
                    (model, "bold"),
                    (f"\n{descriptions[model]}", "dim"),
                ),
                id=model,
            )
            for model in SELECTABLE_MODELS
        ]
        with Vertical(id="dialog", classes="select-dialog"):
            yield Static("Select model", classes="dialog-title")
            yield OptionList(*options, id="model-options")
            yield Static("↑/↓ choose · Enter apply · Esc cancel", classes="input-guide")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        values = [option.id for option in options.options]
        options.highlighted = (
            values.index(self.current) if self.current in values else 0
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

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
            yield Static(
                "↑/↓ choose · Enter resume · Esc cancel", classes="input-guide"
            )

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
        options.add_options(
            Option(item, id=str(index)) for index, item in enumerate(matches)
        )
        options._capslock_matches = matches

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        matches = getattr(event.option_list, "_capslock_matches", [])
        if event.option.id is not None:
            self.dismiss(matches[int(event.option.id)])

    def action_cancel(self) -> None:
        self.dismiss(None)
