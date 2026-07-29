from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from capslock.cli.command_ui import Choice, ConsoleCommandUI
from capslock.cli.commands import (
    COMMANDS,
    CommandAvailability,
    CommandOutcomeKind,
)
from capslock.cli.context import CliContext
from capslock.cli.command_handlers.session import copy_answer, resume
from capslock.domain import RunKind
from capslock.policy import WorkspacePolicy
from capslock.runtime.model import ModelMessage, ModelResponse, ModelToolCall
from capslock.runtime.side_question import run_side_question
from capslock.storage.repositories import WorkspaceRepositories
from capslock.theme import make_console
from capslock.tooling import ExecutionContext, ToolOutcome, ToolRuntime, define_tool


REQUIRED = {
    "/init",
    "/resume",
    "/btw",
    "/compact",
    "/new",
    "/copy",
    "/export",
    "/branch",
    "/context",
    "/worktree",
    "/rewind",
    "/stats",
    "/doctor",
}


class _UI:
    def __init__(self, selected: str | None = None) -> None:
        self.selected = selected
        self.copied: list[str] = []

    async def select(self, title: str, choices: list[Choice]) -> str | None:
        return self.selected or (choices[0].value if choices else None)

    async def confirm(self, *args, **kwargs) -> bool:
        return False

    async def show(self, *args) -> None:
        return None

    async def show_markdown(self, *args) -> None:
        return None

    async def input_text(self, *args) -> str | None:
        return None

    async def request_plan_entry(self, objective: str) -> bool:
        return False

    async def request_plan_approval(self, **kwargs):
        from capslock.cli.command_ui import PlanApprovalResult

        return PlanApprovalResult(self.selected or "feedback")

    async def copy(self, content: str) -> str:
        self.copied.append(content)
        return "test"


def test_command_catalog_is_typed_and_has_no_new_aliases() -> None:
    by_path = {item.path: item for item in COMMANDS}
    assert REQUIRED <= set(by_path)
    assert not ({"/continue", "/clear", "/fork"} & set(by_path))
    assert all(item.usage and item.group and item.handler for item in COMMANDS)
    assert by_path["/btw"].availability is CommandAvailability.IMMEDIATE
    assert by_path["/resume"].availability is CommandAvailability.IDLE_ONLY
    assert by_path["/init"].availability is CommandAvailability.IDLE_ONLY


def test_inline_command_ui_renders_markdown() -> None:
    async def scenario() -> str:
        output = io.StringIO()
        console = make_console(file=output, width=60, color_system=None)
        await ConsoleCommandUI(console).show_markdown(
            "BTW", "# Result\n\n**important**\n\n- first item"
        )
        return output.getvalue()

    rendered = asyncio.run(scenario())
    assert "Result" in rendered
    assert "important" in rendered
    assert "first item" in rendered
    assert "**important**" not in rendered


def test_inline_side_question_uses_normal_agent_markdown_card() -> None:
    async def scenario() -> str:
        output = io.StringIO()
        console = make_console(file=output, width=60, color_system=None)
        await ConsoleCommandUI(console).show_agent_response(
            "BTW", "what changed?", "**important**\n\n- first item"
        )
        return output.getvalue()

    rendered = asyncio.run(scenario())
    assert "/btw what changed?" in rendered
    assert "important" in rendered
    assert "first item" in rendered
    assert "**important**" not in rendered
    assert "◆ CapsLock" in rendered
    assert "▌" in rendered


def test_side_question_runner_exposes_tools_and_redacts_checkpoints() -> None:
    class Model:
        def __init__(self) -> None:
            self.tools = []
            self.calls = 0

        async def complete(self, *, model, messages, tools):
            self.tools = tools
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    ModelMessage(
                        None,
                        (ModelToolCall("call", "lookup", '{"query":"private"}'),),
                    )
                )
            return ModelResponse(ModelMessage("**rendered answer**"))

    class Journal:
        def __init__(self) -> None:
            self.checkpoints = []
            self.invocations = []
            self.tool_calls = []
            self._step = 0

        async def create_step(self, run_id, kind):
            self._step += 1
            return SimpleNamespace(id=f"step-{self._step}")

        async def finish_step(self, step_id, *, status, checkpoint=None, error=None):
            self.checkpoints.append(checkpoint)
            return SimpleNamespace(id=step_id)

        async def start_tool_invocation(self, **values):
            self.invocations.append(values)
            return "invocation"

        async def update_tool_invocation(self, identifier, **values):
            return None

        async def record_tool_call(
            self, run_id, name, arguments, ok, summary, duration_ms
        ):
            self.tool_calls.append((arguments, summary))

        async def finish_tool_invocation(self, identifier, **values):
            self.invocation_result = values

    async def scenario() -> None:
        model, journal = Model(), Journal()

        async def lookup(context, arguments):
            return ToolOutcome.success({"answer": "found"})

        tools = ToolRuntime(
            [
                define_tool(
                    "lookup",
                    "lookup",
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    lookup,
                )
            ]
        )
        session = SimpleNamespace(
            chat_model=model,
            model="test-model",
            tools=tools,
            journal=journal,
            max_tool_rounds=2,
            _run_context=lambda run_id, **kwargs: ExecutionContext(
                session_id="session",
                run_id=run_id,
                policy=WorkspacePolicy(Path(".")),
                event=lambda *args, **kwargs: None,
                actions=object(),
            ),
        )
        result = await run_side_question(
            session,
            "side-run",
            [{"role": "user", "content": "ephemeral question"}],
            emit=lambda *args: asyncio.sleep(0),
        )
        assert result.text == "**rendered answer**"
        assert model.tools
        assert model.calls == 2
        assert journal.checkpoints == [None, None, None]
        assert journal.invocations[0]["arguments"] == {}
        assert journal.tool_calls == [({}, "")]
        assert journal.invocation_result["result_preview"] == ""

    asyncio.run(scenario())


def test_hidden_runs_are_not_transcript_or_primary_cost(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test")
            seed = await repositories.runs.create_hidden(
                session.id, kind=RunKind.SESSION_SEED, status="completed"
            )
            await repositories.sessions.append_message(
                session.id, seed.id, "assistant", "visible"
            )
            side = await repositories.runs.create_hidden(
                session.id,
                kind=RunKind.SIDE_QUESTION,
                status="completed",
                input_tokens=10,
                output_tokens=5,
                cost_usd=1,
            )
            await repositories.sessions.append_message(
                session.id, side.id, "assistant", "must stay hidden"
            )
            assert [
                item["content"]
                for item in await repositories.sessions.transcript(session.id)
            ] == ["visible"]
            assert await repositories.runs.session_cost(session.id) == (0, 0, 0.0)
            usage = {
                item["kind"]: item
                for item in await repositories.runs.usage_breakdown(session.id)
            }
            assert usage["side_question"]["input_tokens"] == 10
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_resume_excludes_current_and_copy_uses_visible_seed_answers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            current = await repositories.sessions.create("test")
            other = await repositories.sessions.create("test")
            seed = await repositories.runs.create_hidden(
                current.id, kind=RunKind.SESSION_SEED, status="completed"
            )
            await repositories.sessions.append_message(
                current.id, seed.id, "assistant", "seed answer"
            )
            ui = _UI(other.id)
            agent = SimpleNamespace(
                session_id=current.id, sessions=repositories.sessions
            )
            context = CliContext(
                Console(),
                agent,
                ui=ui,
                application=SimpleNamespace(repositories=repositories),
            )
            outcome = await resume(context, ["/resume"], "/resume")
            assert outcome.kind is CommandOutcomeKind.SWITCH_SESSION
            assert outcome.session_id == other.id
            await copy_answer(context, ["/copy"], "/copy")
            assert ui.copied == ["seed answer"]
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_branch_records_lineage_and_seed_messages(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            parent = await repositories.sessions.create("test")
            run = await repositories.runs.create_hidden(
                parent.id, kind=RunKind.SESSION_SEED, status="completed"
            )
            await repositories.sessions.append_message(
                parent.id, run.id, "user", "question"
            )
            await repositories.sessions.append_message(
                parent.id, run.id, "assistant", "answer"
            )
            child = await repositories.sessions.derive(
                parent.id, title="Branch", derivation_kind="branch"
            )
            lineage = await repositories.database.fetch_one(
                "SELECT * FROM session_lineage WHERE session_id=?", (child.id,)
            )
            assert lineage["parent_session_id"] == parent.id
            assert [
                item["content"]
                for item in await repositories.sessions.transcript(child.id)
            ] == ["question", "answer"]
        finally:
            await repositories.close()

    asyncio.run(scenario())
