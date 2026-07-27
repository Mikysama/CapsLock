"""Textual fullscreen application and CLI adapters."""

from __future__ import annotations

import asyncio
import io
import shlex
from typing import Any, TypeVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea

from ...application.foreground import (
    AuthorizerBindings,
    ControllerEvent,
    ControllerEventKind,
    ForegroundRunController,
)
from ...domain import (
    ActionRecord,
    ActionStatus,
    ApprovalDecision,
    ApprovalChoice,
    AgentEventKind,
    BudgetRequest,
    BudgetSnapshot,
    SessionInfo,
)
from ...permissions import PermissionMode
from ...theme import make_console
from .. import actions
from ..commands import COMMANDS, command_descriptions, command_menu_completions
from ..commands import CommandOutcome, CommandOutcomeKind
from ..context import CliContext
from ..command_ui import ConsoleCommandUI
from ..dispatch import dispatch_slash_command
from .models import (
    TuiState,
    add_system_message,
    add_user_message,
    history_state,
    reduce_event,
    remove_queue_item,
    set_queue_running,
    toggle_details,
)
from .presentation import present_action
from .screens import (
    ApprovalScreen,
    ConfirmScreen,
    ContentScreen,
    EnterPlanModeScreen,
    MarkdownContentScreen,
    HistorySearchScreen,
    InputRequestScreen,
    ModelScreen,
    PermissionApprovalScreen,
    PermissionScreen,
    PlanApprovalScreen,
    SessionPickerScreen,
    SideQuestionScreen,
    TextPromptScreen,
)
from .widgets import (
    ActivityBar,
    BottomArea,
    CompletionBar,
    Composer,
    QueueBar,
    SessionHeader,
    StatusBar,
    TranscriptView,
)


_Result = TypeVar("_Result")
_ACTIVITY_FPS = 12


class FullscreenCommandUI:
    """CommandUI adapter backed by Textual modal screens."""

    def __init__(self, app: "CapsLockApp") -> None:
        self.app = app
        self.presented = False

    async def select(self, title: str, choices) -> str | None:
        if not choices:
            return None
        detail = "\n".join(
            f"{i}. {item.label} {item.detail}" for i, item in enumerate(choices, 1)
        )
        value = await self.app._modal_wait(
            TextPromptScreen(
                title + "\n" + detail, placeholder="Number (blank cancels)"
            )
        )
        if not value:
            return None
        try:
            return choices[int(value) - 1].value
        except (ValueError, IndexError):
            raise ValueError("invalid selection") from None

    async def confirm(self, title: str, detail: str, *, default: bool = False) -> bool:
        return bool(await self.app._modal_wait(ConfirmScreen(title, detail)))

    async def show(self, title: str, content: str) -> None:
        self.presented = True
        self.app.push_screen(ContentScreen(title, content))

    async def show_markdown(self, title: str, content: str) -> None:
        self.presented = True
        self.app.push_screen(MarkdownContentScreen(title, content))

    async def show_agent_response(
        self, title: str, question: str, content: str, tools=()
    ) -> None:
        self.presented = True
        self.app.push_screen(SideQuestionScreen(question, content, tools))

    async def input_text(self, title: str, prompt: str) -> str | None:
        return await self.app._modal_wait(TextPromptScreen(title, placeholder=prompt))

    async def request_plan_entry(self, objective: str) -> bool:
        return bool(await self.app._modal_wait(EnterPlanModeScreen(objective)))

    async def request_plan_approval(
        self,
        *,
        objective: str,
        content: str,
        revision: int,
        sha256: str,
        permission_mode: str,
    ):
        return await self.app._modal_wait(
            PlanApprovalScreen(
                objective=objective,
                content=content,
                revision=revision,
                sha256=sha256,
                permission_mode=permission_mode,
            )
        )

    async def copy(self, content: str) -> str:
        return await ConsoleCommandUI(self.app.context.console).copy(content)


CSS = """
App {
    background: ansi_default;
}
Screen {
    background: transparent;
    color: #DCE6F2;
}
#main {
    width: 100%;
    height: 100%;
}
#session-header {
    height: auto;
    min-height: 2;
    padding: 0 2;
    border-bottom: solid #3D4F61;
    background: transparent;
}
#transcript {
    height: 1fr;
    padding: 1 2;
    scrollbar-color: #52687C;
    scrollbar-background: transparent;
}
.message {
    width: 100%;
    height: auto;
    margin-bottom: 1;
    padding: 0 1;
}
.message.user {
    border-left: thick #8CB9DC;
    background: #E0E0E0;
    padding: 1;
}
.message.assistant {
    border-left: thick #5F8FB8;
    padding-left: 1;
}
.message.reasoning {
    color: #718397;
    padding-left: 2;
}
.message.tools {
    color: #A9B8C8;
    padding-left: 2;
}
.message.system {
    color: #A9B8C8;
    border-left: thick #52687C;
    padding-left: 1;
}
#bottom-area {
    height: auto;
    max-height: 20;
    background: transparent;
}
#queue-bar {
    height: auto;
    max-height: 3;
    padding: 0 2;
    background: transparent;
}
#completions {
    height: auto;
    max-height: 10;
    padding: 0 2;
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-size-vertical: 1;
    scrollbar-color: #52687C;
    scrollbar-background: transparent;
    background: transparent;
}
#completions .completion-content {
    width: 100%;
    height: auto;
    text-wrap: nowrap;
    text-overflow: ellipsis;
    background: transparent;
}
#composer {
    height: 5;
    min-height: 3;
    max-height: 8;
    margin: 0 1;
    border: solid #52687C;
    background: transparent;
    color: #DCE6F2;
}
#composer:focus {
    border: solid #8CB9DC;
}
#composer .text-area--cursor-line,
#composer .text-area--cursor-gutter,
#composer .text-area--matching-bracket {
    background: transparent;
}
#composer .text-area--cursor {
    background: transparent;
    color: #DCE6F2;
    text-style: underline;
}
#activity {
    height: 1;
    padding: 0 2;
}
#status {
    height: 1;
    padding: 0 2;
    color: #718397;
}
#too-small {
    display: none;
    layer: overlay;
    width: 100%;
    height: 100%;
    content-align: center middle;
    background: transparent;
    color: #C4A96B;
}
ModalScreen {
    align: center middle;
    background: transparent;
}
#dialog {
    width: 78%;
    max-width: 100;
    height: auto;
    max-height: 82%;
    padding: 1 2;
    border: solid #52687C;
    background: transparent;
}
.confirm-dialog { width: 64; }
.approval-dialog { width: 86%; min-height: 14; }
.select-dialog { width: 76; min-height: 16; }
.session-dialog { width: 86; height: 70%; }
.content-dialog { width: 86%; height: 76%; }
.dialog-title {
    text-style: bold;
    color: #DCE6F2;
    margin-bottom: 1;
}
.permission-title { color: #C4A96B; }
.plan-title { color: #72AFA7; }
.dialog-detail { margin-bottom: 1; }
.dialog-scroll { height: 1fr; }
.approval-preview {
    height: 1fr;
    min-height: 5;
    margin: 1 0;
    border: solid #3D4F61;
    padding: 0 1;
}
.plan-entry-dialog { width: 72; min-height: 22; }
.plan-entry-dialog OptionList { height: 5; }
.plan-approval-dialog { width: 90%; height: 86%; }
.plan-preview {
    height: 1fr;
    min-height: 8;
    margin: 1 0;
    border-top: dashed #3D4F61;
    border-bottom: dashed #3D4F61;
    padding: 0 1;
}
.plan-approval-dialog OptionList { height: 8; }
#plan-feedback { margin-top: 1; }
.dialog-actions {
    height: 3;
    align-horizontal: right;
    margin-top: 1;
}
.dialog-actions Button { margin-left: 1; }
.input-guide {
    color: #718397;
    text-style: italic;
    margin-top: 1;
}
"""


class CapsLockApp(App[int]):
    CSS = CSS
    TITLE = "CapsLock"
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Cancel/exit", priority=True),
        Binding("ctrl+o", "toggle_details", "Details", priority=True),
        Binding("ctrl+r", "history_search", "History", priority=True),
        Binding("tab", "complete", "Complete", priority=True),
    ]

    def __init__(self, context: CliContext, *, status_enabled: bool = True) -> None:
        super().__init__(ansi_color=True)
        self.context = context
        self.agent_session = context.session
        self.status_enabled = status_enabled
        self.state = TuiState()
        self.session: SessionInfo | None = None
        self.controller = ForegroundRunController(
            self.agent_session,
            consumer=self._controller_event,
            authorize_limit=self._authorize_limit,
        )
        self._authorizers = AuthorizerBindings(
            self.agent_session,
            action_authorizer=self._authorize_action,
            budget_authorizer=self._authorize_budget,
        )
        self._input_history: list[str] = []
        self._history_index = 0
        self._completion_values: list[str] = []
        self._completion_items: list[tuple[str, str]] = []
        self._completion_index = 0
        self._too_small = False
        self._activity_timer: Any = None
        self._sync_lock = asyncio.Lock()
        self._side_question_task: asyncio.Task[Any] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="main"):
            yield SessionHeader(id="session-header")
            yield TranscriptView()
            yield BottomArea(id="bottom-area")
        yield Static(
            "Terminal too small\nCapsLock needs at least 48 columns × 14 rows",
            id="too-small",
        )

    async def on_mount(self) -> None:
        queries = self.context.require_queries()
        self.session = await queries.session(self.agent_session.session_id)
        transcript = await queries.transcript(self.agent_session.session_id)
        self.state = history_state(transcript)
        if not transcript:
            self.state = add_system_message(
                self.state,
                "Welcome to CapsLock\nType / for commands or $ to load a Skill.",
            )
        self._input_history = [
            str(item.get("content", ""))
            for item in transcript
            if item.get("role") == "user" and item.get("content")
        ]
        self._history_index = len(self._input_history)
        await self._authorizers.__aenter__()
        await self.controller.start()
        self._activity_timer = self.set_interval(
            1 / _ACTIVITY_FPS, self._refresh_activity, pause=True
        )
        await self._sync()
        self.query_one(Composer).focus()

    async def on_unmount(self) -> None:
        if self._activity_timer is not None:
            self._activity_timer.stop()
        await self._authorizers.__aexit__(None, None, None)
        await self.controller.shutdown()

    async def on_resize(self, event: Any) -> None:
        self._too_small = event.size.width < 48 or event.size.height < 14
        self.query_one("#too-small", Static).display = self._too_small
        self.query_one("#main", Vertical).display = not self._too_small
        await self._sync_chrome()

    async def on_composer_submitted(self, event: Composer.Submitted) -> None:
        text = event.text.strip()
        if not text:
            return
        composer = self.query_one(Composer)
        composer.clear()
        self.query_one(CompletionBar).update_candidates(())
        self._completion_values = []
        self._completion_items = []
        if text.startswith("/"):
            # Commands such as /model wait for a modal screen result.  Keeping
            # that wait inside the App's message handler blocks the message
            # pump that must pop the screen, leaving the selector frozen.
            self.run_worker(
                self._dispatch_command(text),
                group="slash-command",
                exclusive=True,
            )
            return
        if self.agent_session.permission_mode is PermissionMode.ASK_FOR_APPROVAL:
            allowed = await self._modal_wait(
                ConfirmScreen("Send this request?", text[:1000])
            )
            if not allowed:
                return
        item = await self.agent_session.enqueue(text)
        self._input_history.append(text)
        self._history_index = len(self._input_history)
        self.state = add_user_message(self.state, item.id, text)
        await self.controller.enqueue_item(item.id, item.question)
        await self._sync()

    async def on_composer_history_requested(
        self, event: Composer.HistoryRequested
    ) -> None:
        if not self._input_history:
            return
        self._history_index = max(
            0,
            min(len(self._input_history), self._history_index + event.direction),
        )
        value = (
            ""
            if self._history_index == len(self._input_history)
            else self._input_history[self._history_index]
        )
        self.query_one(Composer).load_text(value)

    def on_composer_completion_requested(
        self, event: Composer.CompletionRequested
    ) -> None:
        if not self._completion_values:
            return
        self._completion_index = (self._completion_index + event.direction) % len(
            self._completion_values
        )
        self.query_one(CompletionBar).update_candidates(
            self._completion_items, selected=self._completion_index
        )

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "composer":
            return
        self._update_completions(event.text_area.text)

    async def on_transcript_view_load_older(
        self, _event: TranscriptView.LoadOlder
    ) -> None:
        await self.query_one(TranscriptView).sync_messages(self.state.messages)

    async def action_interrupt(self) -> None:
        if self._side_question_task is not None and not self._side_question_task.done():
            self._side_question_task.cancel()
            self.state = add_system_message(
                self.state, "Cancelling the side question…", status="cancelled"
            )
            await self._sync()
            return
        if await self.controller.cancel():
            self.state = add_system_message(
                self.state, "Cancelling the active run…", status="cancelled"
            )
            await self._sync()
            return
        self.exit(0)

    async def action_toggle_details(self) -> None:
        self.state = toggle_details(self.state)
        await self._sync()

    async def action_history_search(self) -> None:
        value = await self._modal_wait(HistorySearchScreen(self._input_history))
        if value is not None:
            self.query_one(Composer).load_text(value)
            self.query_one(Composer).focus()

    def action_complete(self) -> None:
        if not self._completion_values:
            return
        value = self._completion_values[self._completion_index]
        self.query_one(Composer).load_text(value + " ")

    async def _controller_event(self, item: ControllerEvent) -> None:
        if item.kind is ControllerEventKind.STARTED and item.work_item_id:
            self.state = set_queue_running(self.state, item.work_item_id)
        elif item.kind is ControllerEventKind.RUN_EVENT and item.event is not None:
            self.state = reduce_event(self.state, item.event)
            if item.event.kind is AgentEventKind.WAITING_INPUT:
                request = item.event.data.get("request", {})
                questions = (
                    request.get("questions", []) if isinstance(request, dict) else []
                )
                answers = await self._modal_wait(InputRequestScreen(questions))
                if answers is not None:
                    await self.context.session.journal.answer_input_request(
                        str(item.event.data["request_id"]),
                        self.context.session.session_id,
                        answers,
                    )
                    run = await self.context.session.runs.require(
                        item.event.run_id,
                        session_id=self.context.session.session_id,
                    )
                    await self.controller.enqueue_item(
                        item.event.work_item_id,
                        run.question,
                        item.event.run_id,
                    )
            elif item.event.kind is AgentEventKind.WAITING_APPROVAL:
                try:
                    plan_request = await self.agent_session.resolve_plan_request(
                        str(item.event.data.get("request_id", ""))
                    )
                except ValueError:
                    plan_request = None
                if plan_request is not None:
                    from ..plans import decide_plan_request_interactively

                    await decide_plan_request_interactively(
                        CliContext(
                            self.context.console,
                            self.agent_session,
                            self.context.queries,
                            ui=FullscreenCommandUI(self),
                            application=self.context.application,
                        ),
                        plan_request,
                    )
                    run = await self.agent_session.runs.require(
                        item.event.run_id,
                        session_id=self.agent_session.session_id,
                    )
                    await self.controller.enqueue_item(
                        item.event.work_item_id,
                        run.question,
                        item.event.run_id,
                    )
                    await self._sync()
                    return
                try:
                    request = await self.agent_session.resolve_permission_request(
                        str(item.event.data.get("request_id", ""))
                    )
                except ValueError:
                    request = None
                if request is not None:
                    decision = await self._modal_wait(
                        PermissionApprovalScreen(request)
                    )
                    await self.agent_session.decide_permission_request(
                        str(request["id"]), decision
                    )
                    run = await self.agent_session.runs.require(
                        item.event.run_id,
                        session_id=self.agent_session.session_id,
                    )
                    await self.controller.enqueue_item(
                        item.event.work_item_id,
                        run.question,
                        item.event.run_id,
                    )
        elif item.kind is ControllerEventKind.CANCELLED:
            self.state = add_system_message(self.state, "Cancelled", status="cancelled")
        elif item.kind is ControllerEventKind.FAILED:
            self.state = add_system_message(
                self.state,
                f"Run failed: {item.error}",
                status="failed",
            )
        elif item.kind is ControllerEventKind.FINISHED and item.work_item_id:
            self.state = remove_queue_item(self.state, item.work_item_id)
        await self._sync()

    async def _authorize_action(
        self, action: ActionRecord
    ) -> ApprovalChoice | ApprovalDecision:
        if self._too_small:
            return ApprovalDecision.REJECT
        result = await self._modal_wait(ApprovalScreen(action))
        return result or ApprovalDecision.REJECT

    async def _authorize_budget(self, request: BudgetRequest) -> bool:
        detail = (
            f"{request.scope} {request.limit_type}: "
            f"{request.current_value:.6g} + {request.reserved_value:.6g} "
            f"> {request.limit_value:.6g} using {request.profile}"
        )
        return bool(
            await self._modal_wait(ConfirmScreen("Allow this model call?", detail))
        )

    async def _authorize_limit(self, snapshot: BudgetSnapshot) -> bool:
        used = snapshot.as_dict()["used"]
        detail = (
            f"{used['tool_rounds']} rounds · {used['tool_calls']} calls · "
            f"{used['tokens']} tokens · ${float(used['budget_usd']):.6f}. "
            "Continue 32 more rounds?"
        )
        return bool(await self._modal_wait(ConfirmScreen("Soft limit reached", detail)))

    async def _modal_wait(self, screen: ModalScreen[_Result]) -> _Result:
        future: asyncio.Future[_Result] = asyncio.get_running_loop().create_future()

        def done(result: _Result) -> None:
            if not future.done():
                future.set_result(result)

        self.push_screen(screen, done)
        return await future

    async def _dispatch_command(self, text: str) -> None:
        parts = shlex.split(text)
        name = parts[0] if parts else ""
        if name in {"/exit", "/quit"}:
            self.exit(0)
            return
        if name == "/help":
            content = "\n".join(
                f"{item.path:<14} {item.description}" for item in COMMANDS
            )
            self.push_screen(ContentScreen("CapsLock commands", content))
            return
        if name == "/permissions" and len(parts) == 1:
            selected = await self._modal_wait(
                PermissionScreen(self.agent_session.permission_mode)
            )
            if selected is not None:
                await self._capture_controller(
                    actions.set_permission_mode, selected.value
                )
            return
        if name == "/model" and len(parts) == 1:
            selected = await self._modal_wait(ModelScreen(self.agent_session.model))
            if selected is not None:
                await self._capture_controller(actions.set_model, selected)
            return
        if name == "/approvals":
            await self._approvals(parts)
            return
        if name == "/memory" and await self._interactive_memory(parts):
            return
        if name == "/queue" and len(parts) == 3 and parts[1] in {"retry", "start"}:
            await self._queue_command(parts[1], parts[2])
            return
        if name == "/btw":
            self._side_question_task = asyncio.current_task()
            try:
                await self._capture_command(text)
            except asyncio.CancelledError:
                self.state = add_system_message(
                    self.state, "Side question cancelled", status="cancelled"
                )
                await self._sync()
            finally:
                self._side_question_task = None
            return
        await self._capture_command(text)

    async def _interactive_memory(self, parts: list[str]) -> bool:
        memory = self.agent_session.memory
        operation = parts[1] if len(parts) > 1 else "list"
        if operation == "add":
            if memory is None:
                self.state = add_system_message(self.state, "Memory is unavailable.")
                await self._sync()
                return True
            content = await self._modal_wait(
                TextPromptScreen("Add workspace memory", placeholder="Memory content")
            )
            if content:
                from ...domain import MemoryScope as _MemoryScope, MemoryType

                item, rules = await memory.add(
                    content=content,
                    memory_type=MemoryType.NOTE,
                    scope=_MemoryScope.WORKSPACE,
                )
                detail = f"Added memory {item.id[:12]}."
                if rules:
                    detail += f" Redacted: {', '.join(rules)}"
                self.state = add_system_message(self.state, detail)
                await self._sync()
            return True
        if operation == "purge" and len(parts) == 3:
            if memory is None:
                self.state = add_system_message(self.state, "Memory is unavailable.")
                await self._sync()
                return True
            confirmed = await self._modal_wait(
                ConfirmScreen(
                    "Permanently purge memory?",
                    f"{parts[2]} will lose its stored content and cannot be restored.",
                )
            )
            if confirmed:
                item = await memory.purge(parts[2])
                self.state = add_system_message(
                    self.state, f"Purged memory {item.id[:12]}."
                )
                await self._sync()
            return True
        if (
            operation == "embeddings"
            and len(parts) == 5
            and parts[2:4] == ["enable", "external"]
        ):
            if memory is None:
                self.state = add_system_message(self.state, "Memory is unavailable.")
                await self._sync()
                return True
            preview = await memory.external_embedding_preview(parts[4])
            detail = (
                f"provider={preview['provider']} · model={preview['model']} · "
                f"policy={preview['data_policy']} · records={preview['record_count']} · "
                f"bytes={preview['byte_count']}\n"
                "Send these memory fields and future recall queries externally?"
            )
            confirmed = await self._modal_wait(
                ConfirmScreen("Enable external embeddings?", detail)
            )
            if confirmed:
                await memory.enable_external_embeddings(parts[4], preview)
                self.state = add_system_message(
                    self.state, "External embeddings enabled."
                )
                await self._sync()
            return True
        return False

    async def _approvals(self, parts: list[str]) -> None:
        items = await self.context.require_queries().actions(
            self.agent_session.session_id,
            statuses={
                ActionStatus.PENDING,
                ActionStatus.APPROVED,
                ActionStatus.RUNNING,
            },
        )
        permission_requests = await self.agent_session.permission_requests(
            status="pending"
        )
        plan_requests = await self.agent_session.plan_requests()
        if len(parts) == 1:
            if not items and not permission_requests and not plan_requests:
                self.push_screen(
                    ContentScreen("Pending approvals", "No pending approvals.")
                )
                return
            lines = []
            for item in items:
                view = present_action(item)
                lines.append(f"{item.id[:12]}  {view.subtitle}\n{view.title}\n")
            for request in permission_requests:
                lines.append(
                    f"{str(request['id'])[:12]}  permission · {request['tool']}\n"
                    f"{request['reason']}\n"
                )
            for request in plan_requests:
                lines.append(
                    f"{request.id[:12]}  plan · {request.kind.value}\n"
                    f"{request.objective or request.plan_id or '-'}\n"
                )
            self.push_screen(ContentScreen("Pending approvals", "\n".join(lines)))
            return
        if len(parts) != 3 or parts[1] not in {"approve", "reject"}:
            self.state = add_system_message(
                self.state, "Usage: /approvals [approve|reject <id>]", status="failed"
            )
            await self._sync()
            return
        try:
            action = await self.agent_session.action_factory("cli").resolve(parts[2])
        except ValueError:
            try:
                plan_request = await self.agent_session.resolve_plan_request(parts[2])
            except ValueError:
                plan_request = None
            if plan_request is not None:
                if parts[1] == "reject":
                    await self.agent_session.decide_plan_request(
                        plan_request.id, "reject"
                    )
                else:
                    from ..plans import decide_plan_request_interactively

                    await decide_plan_request_interactively(
                        CliContext(
                            self.context.console,
                            self.agent_session,
                            self.context.queries,
                            ui=FullscreenCommandUI(self),
                            application=self.context.application,
                        ),
                        plan_request,
                    )
                if plan_request.run_id:
                    run = await self.agent_session.runs.require(
                        plan_request.run_id,
                        session_id=self.agent_session.session_id,
                    )
                    await self.controller.enqueue_item(
                        run.work_item_id, run.question, run.id
                    )
                return
            request = await self.agent_session.resolve_permission_request(parts[2])
            decision = ApprovalChoice.REJECT
            if parts[1] == "approve":
                decision = await self._modal_wait(
                    PermissionApprovalScreen(request)
                )
            await self.agent_session.decide_permission_request(
                str(request["id"]), decision
            )
            run = await self.agent_session.runs.require(
                str(request["run_id"]),
                session_id=self.agent_session.session_id,
            )
            await self.controller.enqueue_item(
                run.work_item_id, run.question, run.id
            )
            return
        decision = ApprovalDecision.REJECT
        if parts[1] == "approve":
            decision = await self._authorize_action(action)
        await self._capture_controller(
            actions.apply_action_decision, action, decision.value
        )

    async def _queue_command(self, operation: str, prefix: str) -> None:
        try:
            if operation == "retry":
                item, run = await self.controller.retry(prefix)
                self.state = add_user_message(self.state, item.id, item.question)
            else:
                await self.controller.start_queued(prefix)
            await self._sync()
        except (ValueError, OSError) as exc:
            self.state = add_system_message(
                self.state, f"Error: {exc}", status="failed"
            )
            await self._sync()

    async def _capture_command(self, text: str) -> None:
        buffer = io.StringIO()
        console = make_console(
            file=buffer, width=max(48, self.size.width - 6), force_terminal=False
        )
        try:
            ui = FullscreenCommandUI(self)
            result = await dispatch_slash_command(
                CliContext(
                    console,
                    self.agent_session,
                    self.context.queries,
                    ui=ui,
                    application=self.context.application,
                ),
                text,
            )
            if result.kind is CommandOutcomeKind.ENQUEUE:
                assert result.work_item_id and result.question
                self.state = add_user_message(
                    self.state, result.work_item_id, result.question
                )
                await self.controller.enqueue_item(
                    result.work_item_id, result.question
                )
            elif result.kind is not CommandOutcomeKind.HANDLED:
                self.exit(result)
                return
        except (ValueError, OSError) as exc:
            console.print(f"Error: {exc}")
        content = buffer.getvalue().rstrip()
        if not ui.presented:
            self.push_screen(
                ContentScreen(
                    text.split(maxsplit=1)[0], content or "Command completed."
                )
            )
        await self._sync_chrome()

    async def _capture_controller(self, function: Any, *args: object) -> None:
        buffer = io.StringIO()
        console = make_console(
            file=buffer, width=max(48, self.size.width - 6), force_terminal=False
        )
        await function(
            CliContext(console, self.agent_session, self.context.queries), *args
        )
        content = buffer.getvalue().rstrip()
        if content:
            self.state = add_system_message(self.state, content)
        await self._sync()

    def _update_completions(self, text: str) -> None:
        candidates: list[tuple[str, str]] = []
        values: list[str] = []
        if text.startswith("/") and " " not in text:
            descriptions = command_descriptions()
            values = command_menu_completions(text)
            candidates = [(value, descriptions[value]) for value in values]
        elif text.startswith("$") and " " not in text:
            prefix = text[1:].casefold()
            for entry in self.agent_session.skills.entries():
                if (
                    entry.enabled
                    and entry.error is None
                    and entry.package is not None
                    and entry.name.casefold().startswith(prefix)
                ):
                    values.append(f"${entry.name}")
                    candidates.append((f"${entry.name}", entry.package.description))
        self._completion_values = values
        self._completion_items = candidates
        self._completion_index = 0
        self.query_one(Composer).completion_count = len(values)
        self.query_one(CompletionBar).update_candidates(candidates, selected=0)

    def _refresh_activity(self) -> None:
        bars = self.query(ActivityBar)
        if bars:
            bars.first().update_state(self.state, enabled=self.status_enabled)

    async def _sync(self) -> None:
        async with self._sync_lock:
            transcript = self.query_one(TranscriptView)
            follow = not transcript.children or transcript.is_vertical_scroll_end
            await transcript.sync_messages(self.state.messages, follow=follow)
            self.query_one(QueueBar).update_queue(self.state.queue)
            await self._sync_chrome()
            if follow:
                self.call_after_refresh(transcript.scroll_end, animate=False)

    async def _sync_chrome(self) -> None:
        if self.session is None:
            return
        width = self.size.width
        current_plan_loader = getattr(self.agent_session, "current_plan", None)
        current_plan = (
            await current_plan_loader() if callable(current_plan_loader) else None
        )
        permission_label = (
            f"⏸ plan mode on · {current_plan[0].status.value} · "
            f"{self.agent_session.permission_mode.value}"
            if current_plan is not None
            else self.agent_session.permission_mode.value
        )
        self.query_one(SessionHeader).update_header(
            title=self.session.title,
            workspace=str(self.agent_session.workspace),
            model=self.agent_session.model,
            permission=permission_label,
            width=width,
        )
        self.query_one(StatusBar).update_status(
            self.state,
            model=self.agent_session.model,
            permission=permission_label,
            workspace=str(self.agent_session.workspace),
            width=width,
            context_limit=self.agent_session.context_budget.input_budget,
        )
        activity = self.query_one(ActivityBar).update_state(
            self.state, enabled=self.status_enabled
        )
        if self._activity_timer is not None:
            if activity:
                self._activity_timer.resume()
            else:
                self._activity_timer.pause()


class _SessionPickerApp(App[str | None]):
    CSS = CSS

    def __init__(self, sessions: list[SessionInfo]) -> None:
        super().__init__(ansi_color=True)
        self.sessions = sessions

    def on_mount(self) -> None:
        self.push_screen(SessionPickerScreen(self.sessions), self.exit)


async def select_session_fullscreen(sessions: list[SessionInfo]) -> str | None:
    if not sessions:
        return None
    return await _SessionPickerApp(sessions).run_async(mouse=True)


async def run_fullscreen_tui(
    context: CliContext, *, status_enabled: bool = True
) -> CommandOutcome:
    result = await CapsLockApp(context, status_enabled=status_enabled).run_async(
        mouse=True
    )
    return (
        result
        if isinstance(result, CommandOutcome)
        else CommandOutcome(CommandOutcomeKind.EXIT)
    )
