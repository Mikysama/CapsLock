"""Builtin success contracts reject incomplete and mistyped provider data."""

from __future__ import annotations

from copy import deepcopy

import pytest

from capslock.tooling.schema import SchemaValidationError, validate_json_schema
from capslock.tooling.tools import workspace_tools


TASK = dict(
    task_id="task",
    subject="Review",
    description="",
    owner=None,
    active_form=None,
    metadata={"custom": [1]},
    status="pending",
    position=0,
    blocked_by=[],
)
MEMORY = dict(
    memory_id="memory",
    content="Use Python",
    type="preference",
    scope="workspace",
    source={"kind": "manual", "ref": None},
    confidence=1.0,
    expires_at=None,
    revision=1,
    citation="[[memory:memory]]",
)
TEAM = dict(
    id="team",
    session_id="session",
    name="Review",
    state="active",
    created_by_run_id=None,
    created_at="2026-09-24",
    stopped_at=None,
)
WORKER = dict(
    id="agent",
    team_id="team",
    name="Reviewer",
    profile_json="{}",
    workspace_mode="snapshot",
    state="idle",
    persistent=1,
    child_session_id=None,
    created_at="2026-09-24",
    updated_at="2026-09-24",
    stopped_at=None,
)
AGENT_TASK = dict(
    id="task",
    parent_run_id="run",
    owner_session_id="session",
    team_id="team",
    assigned_worker_id=None,
    plan_task_id=None,
    objective="Review",
    contract_json="{}",
    contract_sha256="abc",
    priority=0,
    state="created",
    child_run_id=None,
    child_workspace=None,
    claim_token=None,
    claim_expires_at=None,
    attempt_count=0,
    error=None,
    created_at="2026-09-24",
    started_at=None,
    finished_at=None,
)
AGENT_STATUS = dict(
    task_id="task", state="created", error=None, child_run_id=None, output=None
)
AGENT_OUTPUT = dict(
    task_id="task",
    state="completed",
    summary="Done",
    evidence=[],
    artifacts=[],
    checks=[],
    usage={"input_tokens": 1},
    verified=True,
    content_trust="untrusted_agent",
    verification_scope={},
    error=None,
    memory_proposals=[],
)
MESSAGE = dict(
    id="mail",
    task_id="task",
    parent_run_id="run",
    team_id=None,
    worker_id=None,
    attempt_id=None,
    sender="parent",
    recipient="child",
    message_kind="instruction",
    payload={"custom": [True, None]},
    payload_sha256="abc",
    status="queued",
    created_at="2026-09-24",
    expires_at=None,
    delivered_at=None,
    acknowledged_at=None,
)
TEAM_MESSAGE = dict(
    team_id="team", broadcast=True, delivered=[MESSAGE], recipient_count=1
)
ACTION = dict(
    action_id="action",
    kind="file_create",
    summary="Create file",
    status="completed",
    result_kind="applied",
    request={"path": "test.txt"},
    result={"path": "test.txt", "operation": "create"},
    error=None,
)

# Literal examples describe handler return data, independently of schema builders.
EXAMPLES = {
    "ack_agent_message": {"acknowledged": True},
    "ask_user": {"answers": {"question": ["a", "b"]}},
    "assign_agent_task": AGENT_STATUS,
    "create_agent_task": AGENT_TASK,
    "create_agent_team": TEAM,
    "create_file": ACTION,
    "create_task": TASK,
    "create_worktree": {
        **ACTION,
        "kind": "worktree_create",
        "result": {"active_workspace": "/tmp/work", "operation": "create"},
    },
    "delegate_agents": {"tasks": [AGENT_OUTPUT], "background": False},
    "edit_file": {**ACTION, "kind": "file_edit"},
    "edit_notebook": {**ACTION, "kind": "file_edit"},
    "enter_plan_mode": {"active": True, "plan_id": "plan", "already_active": True},
    "exit_worktree": {
        **ACTION,
        "kind": "worktree_exit",
        "result": {"active_workspace": "/tmp/work", "operation": "keep"},
    },
    "follow_up_agent": AGENT_STATUS,
    "get_agent_task": AGENT_STATUS,
    "get_agent_team": {
        "team": TEAM,
        "workers": [WORKER],
        "tasks": [AGENT_TASK],
        "attempts": [],
        "approvals": [],
        "budget": [],
    },
    "get_memory": MEMORY,
    "get_plan": {
        "plan_id": "plan",
        "objective": "Review",
        "status": "draft",
        "revision": 1,
        "sha256": "abc",
        "content": "Review files",
    },
    "get_task": TASK,
    "git_diff": {"output": ""},
    "git_status": {"output": " M test.py"},
    "glob_files": {
        "pattern": "*.py",
        "path": ".",
        "files": ["test.py"],
        "count": 1,
        "truncated": False,
        "backend": "ripgrep",
        "stop_reason": None,
    },
    "list_external_sources": [
        {
            "source_id": "source",
            "url": "https://example.com",
            "title": "Example",
            "excerpt": "text",
            "fetched_at": "2026-09-24",
            "untrusted": True,
            "suspicious": False,
        }
    ],
    "list_files": {
        "path": ".",
        "entries": [{"path": "test.py", "type": "file"}],
        "files": ["test.py"],
        "count": 1,
        "offset": 0,
        "next_offset": None,
        "truncated": False,
        "stop_reason": None,
    },
    "list_tasks": {"tasks": [TASK]},
    "load_skill": {
        "name": "review",
        "description": "Review files",
        "scope": "workspace",
        "digest": "abc",
        "instructions": "Read first",
        "resources": [{"path": "guide.md", "size": 4, "kind": "text"}],
    },
    "process_output": {
        "process_id": "proc",
        "status": "running",
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "stdout_offset": 0,
        "stderr_offset": 0,
        "progress_bytes": 0,
        "truncated": False,
    },
    "process_stop": {"process_id": "proc", "status": "stopped", "exit_code": -15},
    "publish_agent_artifact": {"published": True, "path": "test.py"},
    "read_agent_messages": {"messages": [MESSAGE]},
    "read_file": {
        "path": "/tmp/test.py",
        "sha256": "abc",
        "start_line": 1,
        "end_line": 2,
        "evidence_id": "ev",
        "total_lines": 2,
        "text": "hello",
    },
    "read_image": {
        "path": "test.png",
        "media_type": "image/png",
        "size_bytes": 123,
        "sha256": "abc",
    },
    "read_notebook": {
        "path": "test.ipynb",
        "sha256": "abc",
        "total_cells": 1,
        "offset": 0,
        "cells": [
            {
                "index": 0,
                "id": None,
                "cell_type": "code",
                "source": "x=1",
                "metadata": {},
                "outputs": "[]",
                "outputs_truncated": False,
            }
        ],
        "truncated": False,
    },
    "read_pdf": {
        "path": "test.pdf",
        "page_count": 1,
        "pages": [{"page": 1, "text": "Hello"}],
        "rendered_pages": 0,
        "sha256": "abc",
    },
    "read_skill_resource": {
        "skill": "review",
        "path": "guide.md",
        "kind": "text",
        "start_line": 1,
        "end_line": 1,
        "text": "Read first",
    },
    "read_tool_artifact": {
        "warning": "Untrusted",
        "artifact_id": "artifact",
        "offset": 0,
        "bytes": 4,
        "content": "test",
        "next_offset": None,
        "has_more": False,
        "sha256": "abc",
    },
    "resume_agent": AGENT_STATUS,
    "search_files": [
        {"id": "ev", "path": "test.py", "start_line": 1, "end_line": 1, "text": "test"}
    ],
    "search_memories": [MEMORY],
    "search_session_history": {
        "warning": "Untrusted",
        "query": "review",
        "count": 1,
        "hits": [
            {
                "document_id": 1,
                "run_id": None,
                "source_kind": "message",
                "source_id": "msg",
                "chunk_ordinal": 0,
                "content": "review",
                "summary": "review",
                "artifact_id": None,
                "score": 0.5,
            }
        ],
    },
    "search_tools": {"tools": ["read_file"], "count": 1, "available_next_turn": True},
    "send_agent_message": MESSAGE,
    "send_team_message": TEAM_MESSAGE,
    "shell": {
        **ACTION,
        "kind": "command",
        "result_kind": "exit_zero",
        "result": {
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "truncated": False,
            "timed_out": False,
        },
    },
    "start_agent": WORKER,
    "stop_agent": WORKER,
    "stop_agent_task": AGENT_STATUS,
    "submit_plan": {"choice": "implement", "plan_id": "plan", "feedback": None},
    "update_plan": {"plan_id": "plan", "revision": 2, "sha256": "abc"},
    "update_task": TASK,
    "web_fetch": {
        **ACTION,
        "kind": "web_fetch",
        "result": {
            "source_id": "source",
            "url": "https://example.com",
            "title": "Example",
            "excerpt": "Text",
            "truncated": False,
            "untrusted": True,
            "suspicious": False,
        },
    },
    "web_search": {
        **ACTION,
        "kind": "web_search",
        "result": {
            "query": "Example",
            "results": [
                {
                    "rank": 1,
                    "source_id": "source",
                    "url": "https://example.com",
                    "title": "Example",
                    "excerpt": "Text",
                    "suspicious": False,
                }
            ],
        },
    },
    "write_file": ACTION,
}


@pytest.fixture(scope="module")
def contracts():
    return {
        name: tool.contract.output_schema
        for name, tool in workspace_tools().catalog._tools.items()
    }


def test_examples_cover_every_public_and_compatibility_builtin(contracts):
    assert set(contracts) == set(EXAMPLES)


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_builtin_accepts_realistic_success_shape(contracts, name):
    validate_json_schema(EXAMPLES[name], contracts[name])


@pytest.mark.parametrize(
    "name,field",
    [
        (name, field)
        for name, example in sorted(EXAMPLES.items())
        for field in (example[0] if isinstance(example, list) else example)
    ],
)
def test_builtin_rejects_missing_stable_fields(contracts, name, field):
    example = deepcopy(EXAMPLES[name])
    record = example[0] if isinstance(example, list) else example
    del record[field]
    with pytest.raises(SchemaValidationError):
        validate_json_schema(example, contracts[name])


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_builtin_rejects_mistyped_stable_fields(contracts, name):
    example = deepcopy(EXAMPLES[name])
    record = example[0] if isinstance(example, list) else example
    key = next(iter(record))
    record[key] = [] if not isinstance(record[key], list) else "not an array"
    with pytest.raises(SchemaValidationError):
        validate_json_schema(example, contracts[name])


@pytest.mark.parametrize(
    "name,example",
    [
        ("stop_agent", AGENT_STATUS),
        ("send_agent_message", TEAM_MESSAGE),
        ("get_agent_task", {**AGENT_STATUS, "output": AGENT_OUTPUT}),
        ("enter_plan_mode", {"choice": "enter", "plan_id": None, "feedback": None}),
        (
            "submit_plan",
            {"choice": "feedback", "plan_id": "plan", "feedback": "Add checks"},
        ),
        (
            "shell",
            {
                **EXAMPLES["shell"],
                "result": {
                    "process_id": "proc",
                    "status": "running",
                    "stdout": "",
                    "stderr": "",
                    "truncated": False,
                    "timed_out": False,
                },
            },
        ),
        (
            "read_notebook",
            {
                **EXAMPLES["read_notebook"],
                "cells": [
                    {
                        "index": 0,
                        "id": None,
                        "cell_type": None,
                        "source": "",
                        "metadata": {},
                    }
                ],
            },
        ),
        (
            "delegate_agents",
            {
                "background": True,
                "tasks": [
                    {
                        **AGENT_OUTPUT,
                        "summary": {
                            "quarantined": True,
                            "source": "agent:task",
                            "bytes": 9,
                            "sha256": "abc",
                            "risk_signals": ["instruction"],
                            "content_available": False,
                            "error_code": "quarantine_unavailable",
                        },
                    }
                ],
            },
        ),
        ("create_file", {**ACTION, "result": None, "result_kind": None}),
        ("get_memory", {**MEMORY, "content": None}),
    ],
)
def test_alternate_and_nullable_success_shapes(contracts, name, example):
    validate_json_schema(example, contracts[name])


@pytest.mark.parametrize(
    "name,path,bad_value",
    [
        ("read_pdf", ("pages", 0, "page"), "one"),
        ("read_notebook", ("cells", 0, "index"), False),
        ("list_files", ("entries", 0, "path"), 3),
        ("list_tasks", ("tasks", 0, "position"), "first"),
        ("search_memories", (0, "source", "kind"), []),
        ("get_agent_team", ("workers", 0, "persistent"), True),
        ("read_agent_messages", ("messages", 0, "payload"), []),
        ("delegate_agents", ("tasks", 0, "usage", "input_tokens"), "many"),
        ("web_search", ("result", "results", 0, "rank"), "first"),
        ("create_file", ("result", "path"), 4),
        ("shell", ("result", "exit_code"), False),
    ],
)
def test_nested_records_are_typed(contracts, name, path, bad_value):
    example = deepcopy(EXAMPLES[name])
    value = example
    for part in path[:-1]:
        value = value[part]
    value[path[-1]] = bad_value
    with pytest.raises(SchemaValidationError):
        validate_json_schema(example, contracts[name])


def test_schema_lookup_fails_closed_and_returns_isolated_copies():
    from capslock.tooling.tools.output_schemas import builtin_output_schema

    with pytest.raises(ValueError, match="missing output schema"):
        builtin_output_schema("new_builtin_without_contract")
    first = builtin_output_schema("create_task")
    first["properties"]["subject"]["type"] = "integer"
    validate_json_schema(TASK, builtin_output_schema("create_task"))
    validate_json_schema(TASK, builtin_output_schema("update_task"))


def test_real_read_write_and_task_handlers_match_contracts(tmp_path):
    import asyncio
    import hashlib
    import json

    from capslock.application.action_system import FileActionHandler
    from capslock.permissions import PermissionMode
    from capslock.policy import WorkspacePolicy
    from capslock.storage.repositories import WorkspaceRepositories
    from capslock.tooling.contracts import ExecutionContext
    from tests.helpers import workspace_run
    from tests.test_actions import coordinator

    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repos)
            policy = WorkspacePolicy(tmp_path)
            actions = coordinator(
                repos,
                session.id,
                prepared.run.id,
                [FileActionHandler(policy)],
                mode=PermissionMode.FULL_ACCESS,
            )
            context = ExecutionContext(
                session_id=session.id,
                run_id=prepared.run.id,
                policy=policy,
                event=lambda *_args, **_kwargs: None,
                actions=actions,
                tasks=repos.tasks,
                permission_mode=PermissionMode.FULL_ACCESS,
            )
            runtime = workspace_tools()

            async def invoke(name, arguments):
                result = await runtime.invoke(name, context, arguments)
                assert result.outcome.ok, (name, result.outcome.error)
                validate_json_schema(
                    result.outcome.data,
                    runtime.catalog._tools[name].contract.output_schema,
                )
                return result.outcome.data

            await invoke("create_file", {"path": "test.txt", "content": "first\n"})
            read = await invoke("read_file", {"path": "test.txt"})
            assert read["text"] == "first"
            await invoke(
                "write_file",
                {
                    "path": "test.txt",
                    "content": "second\n",
                    "expected_sha256": read["sha256"],
                },
            )
            await invoke(
                "edit_file",
                {"path": "test.txt", "old_text": "second", "new_text": "third"},
            )
            assert (tmp_path / "test.txt").read_text() == "third\n"
            await invoke("list_files", {"path": "."})
            await invoke("glob_files", {"path": ".", "pattern": "*.txt"})
            await invoke("search_files", {"path": ".", "query": "third"})
            notebook = {
                "cells": [
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": ["a=1"],
                        "outputs": [],
                    }
                ]
            }
            (tmp_path / "test.ipynb").write_text(json.dumps(notebook))
            await invoke(
                "read_notebook", {"path": "test.ipynb", "include_outputs": True}
            )
            digest = hashlib.sha256((tmp_path / "test.ipynb").read_bytes()).hexdigest()
            await invoke(
                "edit_notebook",
                {
                    "path": "test.ipynb",
                    "expected_sha256": digest,
                    "mode": "replace",
                    "index": 0,
                    "source": "a=2",
                },
            )
            created = await invoke("create_task", {"subject": "Review"})
            await invoke("get_task", {"task_id": created["task_id"]})
            await invoke("list_tasks", {})
            await invoke("list_tasks", {"task_id": created["task_id"]})
            await invoke(
                "update_task",
                {
                    "task_id": created["task_id"],
                    "owner": None,
                    "metadata": {"custom": {"value": True}},
                },
            )
        finally:
            await repos.close()

    asyncio.run(scenario())


def test_real_process_handlers_match_running_and_finished_contracts(tmp_path):
    import asyncio
    import sys

    from capslock.policy import WorkspacePolicy
    from capslock.shell import SandboxedCommand, SessionProcessManager
    from capslock.tooling.contracts import ExecutionContext

    async def scenario():
        manager = SessionProcessManager()
        temporary = tmp_path / "process-temp"
        temporary.mkdir()
        context = ExecutionContext(
            session_id="session",
            run_id="run",
            policy=WorkspacePolicy(tmp_path),
            event=lambda *_args, **_kwargs: None,
            actions=object(),
            process_manager=manager,
        )
        runtime = workspace_tools()
        try:
            job = await manager.start(
                "session",
                SandboxedCommand(
                    (
                        sys.executable,
                        "-c",
                        "import time; print('ready', flush=True); time.sleep(30)",
                    ),
                    tmp_path,
                    temporary,
                ),
            )
            result = await runtime.invoke(
                "process_output", context, {"process_id": job.id, "wait_ms": 1000}
            )
            assert result.outcome.ok, result.outcome.error
            assert result.outcome.data["exit_code"] is None
            stopped = await runtime.invoke(
                "process_stop", context, {"process_id": job.id}
            )
            assert stopped.outcome.ok, stopped.outcome.error
            result = await runtime.invoke(
                "process_output", context, {"process_id": job.id, "wait_ms": 0}
            )
            assert result.outcome.ok, result.outcome.error
            assert isinstance(result.outcome.data["exit_code"], int)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_real_agent_repository_records_and_message_routes_match_contracts(tmp_path):
    import asyncio

    from capslock.collaboration import AgentWorkspaceManager, CollaborationService
    from capslock.collaboration.models import MailboxMessageKind
    from capslock.policy import WorkspacePolicy
    from capslock.storage.repositories import WorkspaceRepositories
    from capslock.tooling.contracts import ExecutionContext
    from tests.helpers import workspace_run

    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repos)
            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(tmp_path),
                repository=repos.collaboration,
            )
            context = ExecutionContext(
                session_id=session.id,
                run_id=prepared.run.id,
                policy=WorkspacePolicy(tmp_path),
                event=lambda *_args, **_kwargs: None,
                actions=object(),
                collaboration=service,
            )
            runtime = workspace_tools()

            async def invoke(name, arguments):
                result = await runtime.invoke(name, context, arguments)
                assert result.outcome.ok, (name, result.outcome.error)
                return result.outcome.data

            team = await invoke("create_agent_team", {"name": "Review"})
            worker = await invoke(
                "start_agent", {"team_id": team["id"], "name": "Reviewer"}
            )
            task = await invoke(
                "create_agent_task",
                {
                    "team_id": team["id"],
                    "objective": "Review",
                    "assignee_agent_id": worker["id"],
                },
            )
            await invoke("get_agent_task", {"task_id": task["id"]})
            await invoke("get_agent_team", {"team_id": team["id"]})
            await invoke(
                "send_agent_message",
                {
                    "target_type": "task",
                    "target_id": task["id"],
                    "kind": "instruction",
                    "payload": {"text": "Review now"},
                },
            )
            await invoke(
                "send_agent_message",
                {
                    "target_type": "agent",
                    "target_id": worker["id"],
                    "payload": {"text": "Review now"},
                },
            )
            await invoke(
                "send_agent_message",
                {
                    "target_type": "team",
                    "target_id": team["id"],
                    "broadcast": True,
                    "payload": {"text": "Review now"},
                },
            )
            message = await service.send_child_message(
                task["id"],
                parent_run_id=prepared.run.id,
                kind=MailboxMessageKind.PROGRESS,
                payload={"progress": 1},
            )
            received = await invoke("read_agent_messages", {"task_id": task["id"]})
            assert received["messages"][0]["id"] == message["id"]
            await invoke("ack_agent_message", {"message_id": message["id"]})
            await invoke("stop_agent", {"target_type": "task", "target_id": task["id"]})
            await invoke(
                "stop_agent", {"target_type": "agent", "target_id": worker["id"]}
            )
        finally:
            await repos.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,valid,invalid",
    [
        (
            "evidence",
            {"path": "test.txt", "sha256": "abc"},
            {"path": 42, "sha256": "abc"},
        ),
        (
            "artifacts",
            {"path": "test.txt", "sha256": "abc", "bytes": 5},
            {"path": "test.txt", "sha256": "abc", "bytes": "five"},
        ),
        (
            "checks",
            {"name": "unit", "status": "passed"},
            {"name": "unit", "status": False},
        ),
        (
            "memory_proposals",
            {
                "content": "Use Python",
                "type": "fact",
                "confidence": 0.9,
                "evidence_ids": ["test.txt"],
            },
            {
                "content": "Use Python",
                "type": "fact",
                "confidence": "high",
                "evidence_ids": ["test.txt"],
            },
        ),
    ],
)
def test_validated_agent_output_nested_records(contracts, field, valid, invalid):
    data = {**AGENT_STATUS, "output": {**AGENT_OUTPUT, field: [valid]}}
    validate_json_schema(data, contracts["get_agent_task"])
    data["output"][field] = [invalid]
    with pytest.raises(SchemaValidationError):
        validate_json_schema(data, contracts["get_agent_task"])
    data["output"][field] = [{}]
    with pytest.raises(SchemaValidationError):
        validate_json_schema(data, contracts["get_agent_task"])


def test_real_agent_verifier_output_preserves_optional_nulls(contracts, tmp_path):
    from capslock.collaboration import AgentOutputVerifier, AgentTaskContract
    from capslock.collaboration.workspace import WorkspaceSnapshot

    (tmp_path / "test.txt").write_text("Python")
    contract = AgentTaskContract.create(
        "run", "Review", allowed_paths=("test.txt",), memory_namespace="review"
    )
    output = AgentOutputVerifier().verify(
        contract,
        WorkspaceSnapshot(tmp_path, tmp_path),
        {
            "summary": "Reviewed",
            "evidence": [{"path": "test.txt", "sha256": None}],
            "artifacts": [{"path": "test.txt", "sha256": None}],
            "checks": [{"name": "review", "status": "passed"}],
            "memory_proposals": [
                {
                    "content": "Use Python",
                    "type": "fact",
                    "confidence": 0.9,
                    "evidence_ids": ["test.txt"],
                    "applies_to_parent": None,
                    "subject": None,
                    "why": None,
                    "how_to_apply": None,
                    "risk_flags": [],
                }
            ],
        },
    )
    validate_json_schema(
        {"tasks": [output.as_dict()], "background": False}, contracts["delegate_agents"]
    )
    validate_json_schema(
        {**AGENT_STATUS, "output": output.as_dict()}, contracts["get_agent_task"]
    )
