"""Independent review of automation JSON and workspace authorization boundaries."""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from rich.console import Console

from capslock.output_schema import (
    OutputSchemaError,
    load_output_schema,
    validate_output,
)
from capslock.permissions import PermissionMode
from capslock.policy import PolicyError, WorkspacePolicy
from capslock.tooling.contracts import (
    ExecutionContext,
    ResolvedToolPolicy,
    ToolOutcome,
    define_tool,
)
from capslock.tooling.permission_policy.engine import PermissionEngine
from capslock.tooling.permission_policy.models import PermissionBehavior
from capslock.tooling.permission_policy.presets import write_workspace_edit_preset


def response_format(schema):
    return {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_output_rejects_non_json_numeric_constants(value):
    with pytest.raises(OutputSchemaError):
        validate_output(value, response_format({"type": "number"}))


def test_schema_unresolved_reference_fails_preflight(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"$ref": "#/$defs/missing"}))
    with pytest.raises(ValueError, match="reference"):
        load_output_schema(path)


def test_output_broken_reference_is_wrapped_as_output_error():
    with pytest.raises(OutputSchemaError):
        validate_output("{}", response_format({"$ref": "#/$defs/missing"}))


def test_output_local_recursive_reference_remains_supported(tmp_path):
    schema = {
        "$defs": {
            "node": {
                "type": "object",
                "properties": {
                    "value": {"type": "integer"},
                    "next": {"$ref": "#/$defs/node"},
                },
                "required": ["value"],
            }
        },
        "$ref": "#/$defs/node",
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    fmt = load_output_schema(path)
    assert validate_output('{"value":1,"next":{"value":2}}', fmt) == {
        "value": 1,
        "next": {"value": 2},
    }


@pytest.mark.parametrize("key", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_schema_remote_references_in_unused_branches_are_rejected(tmp_path, key):
    path = tmp_path / "schema.json"
    path.write_text(
        json.dumps({"type": "object", "$defs": {"unused": {key: "file:///etc/passwd"}}})
    )
    with pytest.raises(ValueError, match="reference"):
        load_output_schema(path)


def test_cli_missing_schema_reference_fails_before_workspace_creation(tmp_path):
    from capslock.cli.app import async_main

    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"$ref": "#/$defs/missing"}))
    output = io.StringIO()
    status = asyncio.run(
        async_main(
            [
                "--workspace",
                str(tmp_path),
                "exec",
                "count",
                "--output-schema",
                str(schema),
            ],
            console=Console(file=output),
        )
    )
    assert status == 2
    assert "reference" in output.getvalue()
    assert not (tmp_path / ".capslock").exists()


def test_preset_preserves_colliding_restrictive_rule(tmp_path):
    path = tmp_path / "permissions.toml"
    path.write_text(
        'permissions_version=2\n[[rules]]\nid="preset_workspace_edit_create_file"\nbehavior="deny"\ntool="create_file"\n'
    )
    with pytest.raises(ValueError, match="conflict"):
        write_workspace_edit_preset(path, enabled=True)
    assert 'behavior="deny"' in path.read_text()


def test_preset_rejects_symlinked_parent(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        write_workspace_edit_preset(link / "permissions.toml", enabled=True)
    assert not (external / "permissions.toml").exists()


@pytest.mark.parametrize("behavior", ["ask", "deny"])
def test_preset_cannot_override_restrictive_rule(tmp_path, behavior):
    async def scenario():
        path = tmp_path / "permissions.toml"
        path.write_text(
            f'permissions_version=2\n[[rules]]\nid="restricted"\nbehavior="{behavior}"\ntool="create_file"\n[rules.constraints]\npath="protected/**"\n'
        )
        write_workspace_edit_preset(path, enabled=True)
        engine = PermissionEngine([("local", path)], object())

        async def execute(context, arguments):
            return ToolOutcome.success({})

        tool = define_tool("create_file", "create", {"type": "object"}, execute)
        context = ExecutionContext(
            session_id="session",
            run_id="run",
            policy=WorkspacePolicy(tmp_path),
            event=lambda *a, **kw: None,
            actions=None,
            permission_mode=PermissionMode.ASK_FOR_APPROVAL,
        )
        decision = await engine.decide(
            tool,
            {"path": "protected/test.txt"},
            ResolvedToolPolicy(external_side_effects=True),
            context,
        )
        assert decision.behavior.value == behavior
        allowed = await engine.decide(
            tool,
            {"path": "ordinary.txt"},
            ResolvedToolPolicy(external_side_effects=True),
            context,
        )
        assert allowed.behavior is PermissionBehavior.ALLOW
        with pytest.raises(PolicyError):
            await engine.decide(
                tool, {"path": "../outside.txt"}, ResolvedToolPolicy(), context
            )
        shell = define_tool("shell", "shell", {"type": "object"}, execute)
        hard = await engine.decide(
            shell,
            {"command": "echo safe", "background": True},
            ResolvedToolPolicy(external_side_effects=True),
            context,
        )
        assert hard.behavior is PermissionBehavior.ASK
        destructive = await engine.decide(
            shell,
            {"command": "rm -rf /"},
            ResolvedToolPolicy(destructive=True),
            context,
        )
        assert destructive.behavior is PermissionBehavior.DENY

    asyncio.run(scenario())


@pytest.mark.parametrize("json_events", [False, True])
def test_cli_schema_success_preserves_structured_json(tmp_path, json_events):
    from capslock.cli.context import CliContext
    from capslock.cli.exec import run_exec
    from capslock.storage.repositories import WorkspaceRepositories
    from tests.helpers import FakeChatModel, answer
    from tests.test_runtime import make_agent

    async def scenario():
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session = await repositories.sessions.create("test-model")
            agent = make_agent(
                tmp_path, repositories, session.id, FakeChatModel(answer('{"count":2}'))
            )
            schema = tmp_path / "schema.json"
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                        "additionalProperties": False,
                    }
                )
            )
            output = io.StringIO()
            status = await run_exec(
                CliContext(Console(file=output), agent),
                "count",
                json_events=json_events,
                quiet=True,
                output_schema=schema,
            )
            assert status == 0
            if json_events:
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                assert events[-1]["schema_version"] == 3
                assert events[-1]["event"] == "completed"
                assert events[-1]["data"]["structured_output"] == {"count": 2}
            else:
                assert json.loads(output.getvalue()) == {"count": 2}
        finally:
            await repositories.close()

    asyncio.run(scenario())
