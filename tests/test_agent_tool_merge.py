"""Merged Agent tools retain routing, ownership, and recovery boundaries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from capslock.collaboration import (
    AgentTaskContract,
    AgentWorkspaceManager,
    CollaborationService,
    MailboxMessageKind,
)
from capslock.policy import WorkspacePolicy
from capslock.storage.repositories import WorkspaceRepositories
from capslock.structured_output import strict_provider_schema
from capslock.tooling.catalog import ToolCatalog
from capslock.tooling.contracts import ExecutionContext
from capslock.tooling.executor import ToolExecutor
from capslock.tooling.tools.collaboration import (
    agent_control_tools,
    child_mailbox_tools,
    resolve_agent_operation,
)
from tests.helpers import workspace_run


@asynccontextmanager
async def _agents(tmp_path: Path):
    repositories = await WorkspaceRepositories.open(
        tmp_path / ".capslock" / "state" / "capslock.sqlite3", workspace=tmp_path
    )
    try:
        session, prepared = await workspace_run(repositories)
        service = CollaborationService(
            workspace_manager=AgentWorkspaceManager(tmp_path),
            repository=repositories.collaboration,
            child_runner=lambda *_args: None,
        )
        team = await service.create_team(
            session.id, "review", created_by_run_id=prepared.run.id
        )
        workers = []
        tasks = []
        for name in ("one", "two"):
            worker = await service.start_agent(
                session_id=session.id, team_id=team["id"], name=name
            )
            contract = AgentTaskContract.create(prepared.run.id, name)
            await service.create_agent_task(
                contract,
                session_id=session.id,
                team_id=team["id"],
                worker_id=worker["id"],
            )
            workers.append(worker)
            tasks.append(contract)
        context = ExecutionContext(
            session_id=session.id,
            run_id=prepared.run.id,
            policy=WorkspacePolicy(tmp_path),
            event=lambda *_args, **_kwargs: None,
            actions=object(),
            collaboration=service,
        )
        yield repositories, context, team, workers, tasks
    finally:
        await repositories.close()


def _executor() -> ToolExecutor:
    return ToolExecutor(ToolCatalog(agent_control_tools()))


@pytest.mark.parametrize("target_type", ["task", "agent"])
def test_stop_routes_explicit_target_and_preserves_other_worker(
    tmp_path: Path, target_type: str
) -> None:
    async def scenario():
        async with _agents(tmp_path) as (repos, context, _team, workers, tasks):
            target = tasks[0].task_id if target_type == "task" else workers[0]["id"]
            result = await _executor().invoke(
                "stop_agent", context, {"target_type": target_type, "target_id": target}
            )
            assert result.outcome.ok, result.outcome.error
            first = await repos.collaboration.get_task(tasks[0].task_id)
            assert first["state"] == "cancelled"
            worker = await repos.collaboration.worker(workers[0]["id"])
            assert (worker["state"] == "stopped") == (target_type == "agent")
            second = await repos.collaboration.get_task(tasks[1].task_id)
            assert second["state"] == "created"

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["instruction", "response", "cancel"])
def test_task_message_preserves_kind(tmp_path: Path, kind: str) -> None:
    async def scenario():
        async with _agents(tmp_path) as (_repos, context, _team, _workers, tasks):
            result = await _executor().invoke(
                "send_agent_message",
                context,
                {
                    "target_type": "task",
                    "target_id": tasks[0].task_id,
                    "kind": kind,
                    "payload": {"text": "continue"},
                },
            )
            assert result.outcome.ok, result.outcome.error
            messages = await context.collaboration.read_child_messages(
                tasks[0].task_id, parent_run_id=context.run_id
            )
            assert len(messages) == 1
            assert messages[0]["message_kind"] == kind
            assert messages[0]["payload"] == {"text": "continue"}

    asyncio.run(scenario())


@pytest.mark.parametrize("target_type", ["agent", "team"])
def test_message_reaches_only_explicit_recipient_scope(
    tmp_path: Path, target_type: str
) -> None:
    async def scenario():
        async with _agents(tmp_path) as (_repos, context, team, workers, tasks):
            arguments = {
                "target_type": target_type,
                "target_id": team["id"] if target_type == "team" else workers[0]["id"],
                "payload": {"text": "review"},
            }
            if target_type == "team":
                arguments["broadcast"] = True
            result = await _executor().invoke("send_agent_message", context, arguments)
            assert result.outcome.ok, result.outcome.error
            assert result.outcome.data["recipient_count"] == (
                2 if target_type == "team" else 1
            )
            for index, task in enumerate(tasks):
                messages = await context.collaboration.read_child_messages(
                    task.task_id, parent_run_id=context.run_id
                )
                assert len(messages) == (
                    1 if index == 0 or target_type == "team" else 0
                )
                if messages:
                    assert messages[0]["message_kind"] == "instruction"
                    assert messages[0]["payload"]["payload"] == {"text": "review"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("name", "target_type"),
    [
        ("stop_agent", "task"),
        ("stop_agent", "agent"),
        ("send_agent_message", "task"),
        ("send_agent_message", "agent"),
        ("send_agent_message", "team"),
    ],
)
def test_foreign_session_cannot_control_merged_targets(
    tmp_path: Path, name: str, target_type: str
) -> None:
    async def scenario():
        async with _agents(tmp_path) as (repos, context, team, workers, tasks):
            arguments = {
                "target_type": target_type,
                "target_id": {
                    "task": tasks[0].task_id,
                    "agent": workers[0]["id"],
                    "team": team["id"],
                }[target_type],
            }
            if name == "send_agent_message":
                arguments["payload"] = {"text": "forbidden"}
                if target_type == "task":
                    arguments["kind"] = "instruction"
                elif target_type == "team":
                    arguments["broadcast"] = True
            result = await _executor().invoke(
                name, replace(context, session_id="foreign-session"), arguments
            )
            assert not result.outcome.ok
            assert (
                "session" in result.outcome.error
                or "controller" in result.outcome.error
            )
            for task in tasks:
                record = await repos.collaboration.get_task(task.task_id)
                assert record["state"] == "created"
                messages = await context.collaboration.read_child_messages(
                    task.task_id, parent_run_id=context.run_id
                )
                assert messages == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation",
    ["old_stop_task", "old_stop_agent", "old_team_message"],
)
def test_historical_calls_remain_executable(tmp_path: Path, operation: str) -> None:
    async def scenario():
        async with _agents(tmp_path) as (_repos, context, team, workers, tasks):
            name, arguments = {
                "old_stop_task": ("stop_agent_task", {"task_id": tasks[0].task_id}),
                "old_stop_agent": ("stop_agent", {"agent_id": workers[0]["id"]}),
                "old_team_message": (
                    "send_team_message",
                    {
                        "team_id": team["id"],
                        "broadcast": True,
                        "payload": {"text": "update"},
                    },
                ),
            }[operation]
            result = await _executor().invoke(name, context, arguments)
            assert result.outcome.ok, result.outcome.error

    asyncio.run(scenario())


def test_task_message_requires_canonical_target_at_execution_and_routing(tmp_path):
    tool = next(
        tool for tool in agent_control_tools() if tool.name == "send_agent_message"
    )
    assert tool.contract.input_schema == tool.schema()["function"]["parameters"]
    assert "task_id" not in tool.contract.input_schema["properties"]

    async def scenario():
        async with _agents(tmp_path) as (repos, context, _team, _workers, tasks):
            arguments = {
                "task_id": tasks[0].task_id,
                "kind": "instruction",
                "payload": {"text": "must not be sent"},
            }
            result = await _executor().invoke("send_agent_message", context, arguments)
            assert not result.outcome.ok
            assert result.outcome.error_code == "invalid_tool_arguments"
            assert result.outcome.executed is False
            assert not await repos.collaboration.receive_mailbox(
                tasks[0].task_id, recipient="child"
            )
            with pytest.raises(ValueError, match="target_type"):
                resolve_agent_operation("send_agent_message", arguments)

    asyncio.run(scenario())


def test_child_reply_preserves_correlation_and_rejects_wrong_recipient(tmp_path):
    async def scenario():
        async with _agents(tmp_path) as (repos, context, _team, _workers, tasks):
            sent = []
            for task in tasks:
                result = await _executor().invoke(
                    "send_agent_message",
                    context,
                    {
                        "target_type": "task",
                        "target_id": task.task_id,
                        "kind": "instruction",
                        "payload": {"text": "inspect"},
                    },
                )
                assert result.outcome.ok, result.outcome.error
                sent.append(result.outcome.data)
            executor = ToolExecutor(
                ToolCatalog(child_mailbox_tools(context.collaboration, tasks[0]))
            )
            reply = await executor.invoke(
                "send_parent_message",
                context,
                {
                    "kind": "response",
                    "payload": {"text": "done"},
                    "reply_to_message_id": sent[0]["id"],
                },
            )
            assert reply.outcome.ok, reply.outcome.error
            assert reply.outcome.data["reply_to_message_id"] == sent[0]["id"]
            stored = await repos.collaboration.mailbox_message(reply.outcome.data["id"])
            assert stored["reply_to_message_id"] == sent[0]["id"]
            before = await repos.collaboration.one(
                "SELECT count(*) AS count FROM agent_mailbox"
            )
            with pytest.raises(ValueError, match="sender and recipient"):
                await context.collaboration.send_child_message(
                    tasks[0].task_id,
                    parent_run_id=context.run_id,
                    kind=MailboxMessageKind.RESPONSE,
                    payload={"text": "wrong thread"},
                    reply_to_message_id=sent[1]["id"],
                )
            after = await repos.collaboration.one(
                "SELECT count(*) AS count FROM agent_mailbox"
            )
            assert after == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("stop_agent", {"target_id": "x"}),
        ("stop_agent", {"target_type": "team", "target_id": "x"}),
        ("stop_agent", {"target_type": "agent", "target_id": "x", "agent_id": "x"}),
        ("stop_agent", {"target_type": "task", "target_id": ""}),
        (
            "send_agent_message",
            {"target_type": "task", "target_id": "x", "payload": {}},
        ),
        (
            "send_agent_message",
            {"target_type": "team", "target_id": "x", "payload": {}},
        ),
        (
            "send_agent_message",
            {
                "target_type": "team",
                "target_id": "x",
                "broadcast": False,
                "payload": {},
            },
        ),
        (
            "send_agent_message",
            {
                "target_type": "agent",
                "target_id": "x",
                "broadcast": True,
                "payload": {},
            },
        ),
        (
            "send_agent_message",
            {
                "target_type": "task",
                "target_id": "x",
                "kind": "instruction",
                "broadcast": True,
                "payload": {},
            },
        ),
        (
            "send_agent_message",
            {"target_type": "agent", "target_id": "x", "kind": "cancel", "payload": {}},
        ),
        (
            "send_agent_message",
            {
                "target_type": "task",
                "target_id": "x",
                "task_id": "x",
                "kind": "instruction",
                "payload": {},
            },
        ),
        (
            "send_agent_message",
            {"task_id": "x", "kind": "instruction", "broadcast": False, "payload": {}},
        ),
    ],
)
def test_ambiguous_or_implicit_routes_fail_before_service_access(
    tmp_path: Path, name: str, arguments: dict
) -> None:
    context = ExecutionContext(
        session_id="session",
        run_id="run",
        policy=WorkspacePolicy(tmp_path),
        event=lambda *_args, **_kwargs: None,
        actions=object(),
        collaboration=None,
    )
    result = asyncio.run(_executor().invoke(name, context, arguments))
    assert not result.outcome.ok
    assert result.outcome.error != "multi-Agent collaboration is not configured"
    assert result.outcome.executed is False
    assert result.outcome.error_code == "invalid_tool_arguments"


def test_hidden_recovery_tools_and_historical_fields_are_not_model_visible() -> None:
    tools = {tool.name: tool for tool in agent_control_tools()}
    for name in ("stop_agent_task", "send_team_message"):
        assert tools[name].contract.model_visible is False
    for name in ("stop_agent", "send_agent_message"):
        parameters = tools[name].schema()["function"]["parameters"]
        assert {"target_type", "target_id"}.issubset(parameters["required"])
        assert not {"agent_id", "task_id"}.intersection(parameters["properties"])


@pytest.mark.parametrize("name", ["stop_agent", "send_agent_message"])
def test_target_type_is_not_inferred_from_existing_id(
    tmp_path: Path, name: str
) -> None:
    async def scenario():
        async with _agents(tmp_path) as (repos, context, _team, workers, tasks):
            for target_type, target_id in (
                ("task", workers[0]["id"]),
                ("agent", tasks[0].task_id),
            ):
                arguments = {"target_type": target_type, "target_id": target_id}
                if name == "send_agent_message":
                    arguments["payload"] = {"text": "wrong recipient type"}
                    if target_type == "task":
                        arguments["kind"] = "instruction"
                result = await _executor().invoke(name, context, arguments)
                assert not result.outcome.ok
                assert result.outcome.error_code != "invalid_tool_arguments"
            worker = await repos.collaboration.worker(workers[0]["id"])
            assert worker["state"] != "stopped"
            for task in tasks:
                record = await repos.collaboration.get_task(task.task_id)
                assert record["state"] == "created"
                assert (
                    await context.collaboration.read_child_messages(
                        task.task_id, parent_run_id=context.run_id
                    )
                    == []
                )

    asyncio.run(scenario())


def test_provider_null_placeholders_keep_canonical_message_route(
    tmp_path: Path,
) -> None:
    async def scenario():
        async with _agents(tmp_path) as (_repos, context, _team, workers, tasks):
            result = await _executor().invoke(
                "send_agent_message",
                context,
                {
                    "target_type": "agent",
                    "target_id": workers[0]["id"],
                    "payload": {"text": "hello"},
                    "reply_to_message_id": None,
                    "kind": None,
                    "broadcast": None,
                },
            )
            assert result.outcome.ok, result.outcome.error
            messages = await context.collaboration.read_child_messages(
                tasks[0].task_id, parent_run_id=context.run_id
            )
            assert len(messages) == 1
            assert messages[0]["message_kind"] == "instruction"

    asyncio.run(scenario())


@pytest.mark.parametrize("target_type", ["agent", "team"])
def test_strict_provider_schema_accepts_non_task_message_kind_null(
    target_type: str,
) -> None:
    tool = next(
        tool for tool in agent_control_tools() if tool.name == "send_agent_message"
    )
    schema = strict_provider_schema(tool.schema()["function"]["parameters"])
    Draft202012Validator(schema).validate(
        {
            "target_type": target_type,
            "target_id": "target",
            "payload": {},
            "reply_to_message_id": None,
            "kind": None,
            "broadcast": True if target_type == "team" else None,
        }
    )


def test_child_mailbox_keeps_original_scoped_team_tool(tmp_path: Path) -> None:
    async def scenario():
        async with _agents(tmp_path) as (_repos, context, _team, workers, tasks):
            tools = child_mailbox_tools(context.collaboration, tasks[0])
            assert "send_agent_message" not in {tool.name for tool in tools}
            executor = ToolExecutor(ToolCatalog(tools))
            result = await executor.invoke(
                "send_team_message",
                context,
                {"recipient_agent_id": workers[1]["id"], "payload": {"text": "hello"}},
            )
            assert result.outcome.ok, result.outcome.error
            assert result.outcome.data["recipient_count"] == 1

    asyncio.run(scenario())
