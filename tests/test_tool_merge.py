"""Consolidated tool surfaces retain safe execution and bounded browsing."""

import asyncio
from types import SimpleNamespace
import pytest

from capslock.policy import WorkspacePolicy
from capslock.tooling.tools import workspace_tools
from capslock.tooling.tools.filesystem.read import list_files
from capslock.tooling.tools.tasks import list_tasks


def test_superseded_file_and_task_tools_stay_out_of_model_discovery():
    tools = workspace_tools()
    tools.discover(["create_file", "get_task"])
    visible = {s["function"]["name"] for s in tools.schemas}
    assert not {"create_file", "get_task"} & visible
    assert "create_file" not in tools.candidates("create_file", 100)
    assert tools.get("create_file") is not None
    assert tools.get("get_task") is not None


def test_list_files_browses_direct_children_with_pagination(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("nested")
    ctx = SimpleNamespace(policy=WorkspacePolicy(tmp_path))
    first = asyncio.run(list_files(ctx, {"path": ".", "limit": 1}))
    assert first.data["count"] == 1
    assert first.data["next_offset"] == 1
    second = asyncio.run(list_files(ctx, {"path": ".", "offset": 1, "limit": 1}))
    paths = first.data["entries"] + second.data["entries"]
    assert {p["path"] for p in paths} == {"a.txt", "sub"}
    assert next(p for p in paths if p["path"] == "sub")["type"] == "directory"
    assert second.data["next_offset"] is None


def test_list_tasks_exact_lookup_is_scoped_and_missing_is_not_empty_success():
    class Tasks:
        async def get(self, identifier, *, session_id):
            assert session_id == "session"
            return None

        async def list(self, *args, **kwargs):
            raise AssertionError("exact lookup must not list every task")

    result = asyncio.run(
        list_tasks(
            SimpleNamespace(tasks=Tasks(), session_id="session"), {"task_id": "missing"}
        )
    )
    assert not result.ok and result.error_code == "task_not_found"


@pytest.mark.parametrize(
    ("rule_tool", "name", "arguments"),
    [
        ("stop_agent", "stop_agent", {"target_type": "task", "target_id": "t"}),
        (
            "send_agent_message",
            "send_agent_message",
            {"target_type": "team", "target_id": "t", "broadcast": True, "payload": {}},
        ),
    ],
)
def test_old_agent_allow_does_not_authorize_new_operation(
    tmp_path, rule_tool, name, arguments
):
    decision = asyncio.run(_permission(tmp_path, rule_tool, "allow", name, arguments))
    assert decision.behavior.value == "ask"


@pytest.mark.parametrize("behavior", ["deny", "ask"])
@pytest.mark.parametrize(
    ("rule_tool", "name", "arguments"),
    [
        ("stop_agent_task", "stop_agent", {"target_type": "task", "target_id": "t"}),
        (
            "send_team_message",
            "send_agent_message",
            {"target_type": "team", "target_id": "t", "broadcast": True, "payload": {}},
        ),
        (
            "create_file",
            "write_file",
            {"path": "new.txt", "expected_sha256": None, "content": "new"},
        ),
        ("get_task", "list_tasks", {"task_id": "t"}),
    ],
)
def test_old_restrictions_apply_to_merged_operation(
    tmp_path, rule_tool, name, arguments, behavior
):
    decision = asyncio.run(_permission(tmp_path, rule_tool, behavior, name, arguments))
    assert decision.reason_code == f"explicit_{behavior}"


async def _permission(tmp_path, rule_tool, behavior, name, arguments):
    from capslock.permissions import PermissionMode
    from capslock.tooling.contracts import ExecutionContext, ResolvedToolPolicy
    from capslock.tooling.permission_policy.engine import PermissionEngine

    class Rules:
        async def session_permission_rules(self, session):
            return [{"tool": rule_tool, "behavior": behavior, "constraints": {}}]

    context = ExecutionContext(
        session_id="s",
        run_id="r",
        policy=WorkspacePolicy(tmp_path),
        event=lambda *a, **k: None,
        actions=object(),
        permission_mode=PermissionMode.ASK_FOR_APPROVAL,
    )
    engine = PermissionEngine([], Rules())
    return await engine.decide(
        workspace_tools().get(name),
        arguments,
        ResolvedToolPolicy(external_side_effects=True),
        context,
    )


def test_disabled_agents_are_not_registered(tmp_path, monkeypatch):
    from capslock.composition import tools as composition
    from capslock.tooling.contracts import ToolSelectionMode

    settings = SimpleNamespace(
        agents=SimpleNamespace(enabled=False),
        shell=SimpleNamespace(enabled=True),
        worktree=SimpleNamespace(enabled=True),
        tools=SimpleNamespace(
            schema_budget_tokens=8000, selection_mode=ToolSelectionMode.SHADOW
        ),
    )
    for name in ("lsp_tools", "mcp_resource_tools", "plugin_tools", "mcp_tools"):
        monkeypatch.setattr(composition, name, lambda _: [])
    runtime = asyncio.run(
        composition.build_tool_runtime(
            settings=settings,
            child_mode=False,
            permission_engine=object(),
            lsp=object(),
            mcp=object(),
            plugins=object(),
        )
    )
    assert "delegate_agents" not in runtime.names
    assert runtime.get("stop_agent") is None


def test_directory_browser_bounds_scan_and_omits_private_files_and_symlinks(tmp_path):
    (tmp_path / "file.txt").write_text("ok")
    (tmp_path / ".env").write_text("secret")
    (tmp_path / "link").symlink_to(tmp_path / "file.txt")
    context = SimpleNamespace(policy=WorkspacePolicy(tmp_path))
    result = asyncio.run(list_files(context, {"path": "."}))
    assert result.data["entries"] == [{"path": "file.txt", "type": "file"}]
    context.policy = WorkspacePolicy(tmp_path, max_files=1)
    bounded = asyncio.run(list_files(context, {"path": "."}))
    assert bounded.data["truncated"] is True
    assert bounded.data["stop_reason"] == "scan_limit"
    assert bounded.data["count"] <= 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": -1},
        {"offset": True},
        {"limit": 0},
        {"limit": 1001},
        {"pattern": "*.py"},
    ],
)
def test_directory_browser_rejects_invalid_pagination_and_patterns(tmp_path, arguments):
    with pytest.raises(ValueError):
        asyncio.run(
            list_files(
                SimpleNamespace(policy=WorkspacePolicy(tmp_path)),
                {"path": ".", **arguments},
            )
        )


def test_catalog_keeps_only_explicitly_granted_historical_definitions():
    runtime = workspace_tools()
    hidden = {"create_file", "get_task", "stop_agent_task", "send_team_message"}
    assert len(runtime.names) == 49
    assert set(runtime.catalog._tools) - runtime.names == hidden
    runtime.discover(hidden)
    assert not hidden.intersection(runtime.candidates("agent task file team", 100))
    assert not hidden.intersection(runtime.search("agent task file team", 100))
    assert runtime.filtered({"write_file"}).get("create_file") is None
    restored = runtime.filtered({"write_file", "create_file"}).combined([])
    assert restored.get("create_file") is not None
    assert restored.names == {"write_file"}


@pytest.mark.parametrize(
    ("name", "arguments", "operation"),
    [
        ("stop_agent", {"target_type": "task", "target_id": "t"}, "stop_agent_task"),
        ("stop_agent_task", {"task_id": "t"}, "stop_agent_task"),
        (
            "send_agent_message",
            {"target_type": "team", "target_id": "t", "broadcast": True, "payload": {}},
            "send_team_message",
        ),
    ],
)
@pytest.mark.parametrize("null_placeholders", [False, True])
def test_merged_and_historical_permission_resume_executes_once(
    tmp_path, name, arguments, operation, null_placeholders
):
    from dataclasses import replace
    import json
    from capslock.domain import AgentEventKind, ApprovalChoice
    from capslock.permissions import PermissionMode
    from capslock.runtime.model import ModelMessage, ModelResponse, ModelToolCall
    from capslock.storage.repositories import WorkspaceRepositories
    from capslock.tooling.contracts import ToolOutcome, adapt_executor
    from capslock.tooling.executor import ToolRuntime
    from capslock.tooling.permission_policy.engine import PermissionEngine
    from capslock.tooling.permission_policy.middleware import PermissionMiddleware
    from tests.helpers import FakeChatModel, answer
    from tests.test_runtime import make_agent, collect

    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "restore.sqlite3", workspace=tmp_path
        )
        calls = []
        try:
            session = await repos.sessions.create("test-model")

            async def execute(context, args):
                calls.append(args)
                return ToolOutcome.success({"stopped": True})

            tool = replace(workspace_tools().get(name), execute=adapt_executor(execute))
            engine = PermissionEngine((), repos.run_journal)
            runtime = ToolRuntime([tool], middleware=(PermissionMiddleware(engine),))
            model_arguments = dict(arguments)
            if null_placeholders and name == "send_agent_message":
                model_arguments["kind"] = None
            model = FakeChatModel(
                ModelResponse(
                    ModelMessage(
                        None,
                        (ModelToolCall("call", name, json.dumps(model_arguments)),),
                    )
                ),
                answer("done"),
            )
            agent = make_agent(
                tmp_path,
                repos,
                session.id,
                model,
                tools=runtime,
                permission_engine=engine,
            )
            agent.permission_mode = PermissionMode.ASK_FOR_APPROVAL
            paused = (await collect(agent, "perform authorized operation"))[-1]
            assert paused.kind is AgentEventKind.WAITING_APPROVAL
            request = await agent.resolve_permission_request(paused.data["request_id"])
            await agent.decide_permission_request(
                request["id"], ApprovalChoice.APPROVE_SESSION
            )
            rules = await repos.run_journal.session_permission_rules(session.id)
            assert [r["tool"] for r in rules] == [operation]
            events = [
                event async for event in agent.resume_paused_stream(paused.run_id)
            ]
            assert events[-1].kind is AgentEventKind.COMPLETED
            assert calls == [arguments]
            with pytest.raises(ValueError, match="not pending"):
                await agent.decide_permission_request(
                    request["id"], ApprovalChoice.APPROVE_SESSION
                )
            assert len(calls) == 1
        finally:
            await repos.close()

    asyncio.run(scenario())
