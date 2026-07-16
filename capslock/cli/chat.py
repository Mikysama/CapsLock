"""Interactive input loop and model-turn continuation."""

from __future__ import annotations

from ..domain import MemoryScope
from ..permissions import PermissionMode
from ..runtime import AgentRuntimeError
from . import actions
from .context import CliContext
from .dispatch import dispatch_slash_command
from .prompt import permission_rprompt, prompt_footer, prompt_session, prompt_tokens
from .render import render_answer, select_choice, startup_banner


WEB_CONTINUATION = (
    "The user approved the Web action and it completed. Call list_external_sources, "
    "then continue the user's previous request using those sources. Do not propose the same search again."
)


def run_chat(context: CliContext, debug: bool) -> int:
    agent, console = context.agent, context.console
    try:
        console.print(startup_banner(agent))
        input_session = prompt_session()
        return _run_chat_loop(context, debug, input_session)
    finally:
        _delete_empty_session(agent)


def _run_chat_loop(context: CliContext, debug: bool, input_session: object) -> int:
    agent, console = context.agent, context.console
    while True:
        try:
            question = input_session.prompt(
                prompt_tokens(agent.permission_mode),
                rprompt=permission_rprompt(agent.permission_mode),
                bottom_toolbar=prompt_footer(),
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if question.startswith("/"):
            if dispatch_slash_command(context, question) == "exit":
                return 0
            continue
        if not question:
            continue
        if agent.permission_mode is PermissionMode.ASK_FOR_APPROVAL:
            choice = select_choice(
                console,
                "Send this request to CapsLock?",
                (("approve", "Approve and send"), ("reject", "Do not send")),
                escape_key="reject",
            )
            if choice != "approve":
                console.print("[warning]Request not sent.[/]")
                continue
        try:
            run_chat_turn(context, question, debug)
        except AgentRuntimeError as exc:
            console.print(f"[error]Error:[/] {exc}")


def _delete_empty_session(agent: object) -> None:
    memory = getattr(agent, "memory", None)
    if memory is not None:
        try:
            if memory.list(scope=MemoryScope.SESSION, include_inactive=True, limit=1):
                return
        except Exception:
            return
    agent.store.delete_session_if_empty(agent.session_id)


def run_chat_turn(context: CliContext, question: str, debug: bool) -> None:
    while True:
        with context.console.status("[running.bold]Agent is analyzing the workspace...[/]"):
            answer = context.agent.ask(question)
        render_answer(context.console, answer, debug)
        actions.render_changes(context, pending_only=True)
        completed = actions.review_pending_external_actions(context, answer.run_id)
        if not completed:
            return
        if all(item.kind in {"web_search", "web_fetch"} for item in completed):
            question = WEB_CONTINUATION
        else:
            return
