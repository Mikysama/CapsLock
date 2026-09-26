"""Optional MCP failures must not prevent the workspace runtime from opening."""

import asyncio
import json
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest

from capslock.configuration import Settings
from capslock.composition.integrations import build_integrations
from capslock.layout import ProjectLayout
from capslock.mcp import McpManager, McpRegistry
from capslock.policy import WorkspacePolicy
from capslock.ports.mcp import ManagedMcpTool


def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
    layout = ProjectLayout.discover(tmp_path)
    layout.project_mcp.parent.mkdir(parents=True, exist_ok=True)
    return McpRegistry(WorkspacePolicy(tmp_path), layout=layout)


@pytest.mark.parametrize(
    "payload", ["{broken", "[]", '{"servers": []}', '{"servers":{"bad":{}}}']
)
def test_bad_mcp_configuration_does_not_abort_integrations(
    tmp_path, monkeypatch, payload
):
    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text(payload)

    async def scenario():
        async with AsyncExitStack() as resources:
            bundle = await build_integrations(
                policy=configured.policy,
                settings=Settings.load(tmp_path),
                layout=configured.layout,
                journal=SimpleNamespace(),
                resources=resources,
                child_mode=False,
            )
            assert bundle.mcp.tools() == ()
            assert bundle.mcp.errors
            assert bundle.processes is not None

    asyncio.run(scenario())


def test_bad_server_does_not_hide_healthy_server_or_block_repair(tmp_path, monkeypatch):
    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text(
        json.dumps(
            {
                "servers": {
                    "bad": {},
                    "good": {"command": "safe", "allowed_tools": ["ping"]},
                }
            }
        )
    )
    manager = McpManager(configured.policy, configured)

    async def connect(name):
        server = configured.get(name)
        spec = ManagedMcpTool(name, "ping", "ping", {"type": "object"}, None, {})
        connection = SimpleNamespace(
            server=server, tools=(spec,), resources=(), stack=AsyncExitStack()
        )
        manager._sessions[name] = connection
        return connection

    monkeypatch.setattr(manager, "_connect", connect)

    async def scenario():
        assert [t.server for t in await manager.initialize()] == ["good"]
        assert "bad" in manager.errors
        with pytest.raises(ValueError):
            configured.servers()  # Explicit validation remains strict.
        configured.layout.project_mcp.write_text(
            json.dumps({"servers": {"bad": {"command": "fixed"}}})
        )
        assert [t.server for t in await manager.initialize()] == ["bad"]
        assert not manager.errors
        await manager.close()

    asyncio.run(scenario())


def test_bad_local_override_never_falls_back_to_project_grant(tmp_path, monkeypatch):
    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text(
        json.dumps(
            {
                "servers": {
                    "restricted": {"command": "safe", "allowed_tools": ["write"]},
                    "good": {"command": "safe"},
                }
            }
        )
    )
    configured.layout.local_mcp.parent.mkdir(parents=True, exist_ok=True)
    configured.layout.local_mcp.write_text(
        json.dumps({"servers": {"restricted": "invalid"}})
    )
    servers = configured.servers(strict=False)
    assert set(servers) == {"good"}
    assert "restricted" in configured.errors


def test_missing_credential_isolated_and_disabled_server_not_resolved(
    tmp_path, monkeypatch
):
    configured = registry(tmp_path, monkeypatch)
    monkeypatch.delenv("CAPSLOCK_TEST_MISSING_MCP_KEY", raising=False)
    configured.layout.local_mcp.parent.mkdir(parents=True, exist_ok=True)
    configured.layout.local_mcp.write_text(
        json.dumps(
            {
                "servers": {
                    "missing": {
                        "command": "safe",
                        "env": {"KEY": "env:CAPSLOCK_TEST_MISSING_MCP_KEY"},
                    },
                    "off": {"enabled": False},
                    "good": {"command": "safe"},
                }
            }
        )
    )
    assert set(configured.servers(strict=False)) == {"good"}
    assert set(configured.errors) == {"missing"}


def test_connection_timeout_and_cancellation_are_distinct(tmp_path, monkeypatch):
    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text('{"servers":{"slow":{"command":"safe"}}}')
    manager = McpManager(configured.policy, configured, timeout_seconds=0.01)

    async def block(name):
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_connect", block)

    async def scenario():
        await asyncio.wait_for(manager.initialize(), 0.3)
        assert "slow" in manager.errors
        task = asyncio.create_task(manager.initialize())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_missing_executable_does_not_abort_workspace_startup(
    tmp_path, monkeypatch, caplog
):
    from capslock.bootstrap import WorkspaceApplication

    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text(
        json.dumps(
            {"servers": {"missing": {"command": str(tmp_path / "not-installed")}}}
        )
    )

    async def scenario():
        application = await WorkspaceApplication.open(
            workspace=tmp_path,
            settings=Settings.load(tmp_path),
            layout=configured.layout,
            client={},
            close_client=False,
        )
        try:
            assert "read_file" in application.session.tools.names
            assert "missing" in application._mcp_manager.errors
            assert "CapsLock will continue" in caplog.text
        finally:
            await application.close()

    asyncio.run(scenario())


def test_invalid_local_file_never_enables_project_servers(tmp_path, monkeypatch):
    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text(
        '{"servers":{"project":{"command":"safe"}}}'
    )
    configured.layout.local_mcp.parent.mkdir(parents=True, exist_ok=True)
    configured.layout.local_mcp.write_text("{")
    assert configured.servers(strict=False) == {}
    assert "configuration" in configured.errors


def test_configuration_read_error_is_diagnostic(tmp_path, monkeypatch):
    from pathlib import Path

    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text("{}")
    read = Path.read_text

    def deny(path, *args, **kwargs):
        if path == configured.layout.project_mcp:
            raise PermissionError("permission denied")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)
    assert configured.servers(strict=False) == {}
    assert "permission denied" in configured.errors["configuration"]


def test_mcp_list_shows_invalid_server_without_hiding_valid_configuration(
    tmp_path, monkeypatch
):
    import io
    from rich.console import Console
    from capslock.cli.actions import mcp_command
    from capslock.cli.context import CliContext

    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text(
        '{"servers":{"bad":{},"good":{"command":"safe"}}}'
    )
    output = io.StringIO()
    context = CliContext(
        Console(file=output),
        SimpleNamespace(policy=configured.policy, workspace=tmp_path),
        application=SimpleNamespace(mcp_statuses=lambda: manager.statuses()),
    )
    manager = McpManager(configured.policy, configured)

    async def fail(name):
        raise RuntimeError("offline test")

    monkeypatch.setattr(manager, "_connect", fail)

    async def scenario():
        await manager.initialize()
        await mcp_command(context, "/mcp list")
        await manager.close()

    asyncio.run(scenario())
    assert "good scope=" in output.getvalue()
    assert "bad: unavailable" in output.getvalue()


def test_invalidated_server_tools_are_removed(tmp_path, monkeypatch):
    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text('{"servers":{"bad":{}}}')
    manager = McpManager(configured.policy, configured)
    closed = []
    stack = AsyncExitStack()
    stack.callback(lambda: closed.append(True))
    manager._sessions["bad"] = SimpleNamespace(stack=stack, tools=(object(),))

    async def scenario():
        assert await manager.initialize() == ()
        assert closed == [True]
        assert "bad" in manager.errors

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", ["{", '{"servers":{"missing":{}}}'])
def test_invalid_config_still_allows_full_workspace_open(
    tmp_path, monkeypatch, payload
):
    from capslock.bootstrap import WorkspaceApplication

    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text(payload)

    async def scenario():
        app = await WorkspaceApplication.open(
            workspace=tmp_path,
            settings=Settings.load(tmp_path),
            layout=configured.layout,
            client={},
            close_client=False,
        )
        try:
            assert "read_file" in app.session.tools.names
            assert app._mcp_manager.errors
            await app.session.tools.refresh_dynamic()
            assert "read_file" in app.session.tools.names
        finally:
            await app.close()

    asyncio.run(scenario())


def test_rejected_remote_and_project_secrets_stay_disabled(tmp_path, monkeypatch):
    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text(
        json.dumps(
            {
                "servers": {
                    "insecure": {
                        "transport": "streamable_http",
                        "url": "http://example.test/mcp",
                    },
                    "credentials": {
                        "command": "safe",
                        "env": {"API_KEY": "never-log-this"},
                    },
                    "good": {"command": "safe"},
                }
            }
        )
    )
    assert set(configured.servers(strict=False)) == {"good"}
    assert set(configured.errors) == {"insecure", "credentials"}
    assert "never-log-this" not in str(configured.errors)


def test_credential_backend_failure_is_optional(tmp_path, monkeypatch):
    from capslock.credentials import CredentialError

    configured = registry(tmp_path, monkeypatch)
    configured.layout.local_mcp.parent.mkdir(parents=True, exist_ok=True)
    configured.layout.local_mcp.write_text(
        '{"servers":{"locked":{"command":"safe","env":{"KEY":"keyring:missing"}}}}'
    )

    def fail(reference):
        raise CredentialError("credential backend unavailable")

    monkeypatch.setattr("capslock.mcp.registry.resolve_credential", fail)
    assert configured.servers(strict=False) == {}
    assert "credential backend unavailable" in configured.errors["locked"]


def test_mcp_status_uses_runtime_failure_and_disabled_policy(tmp_path, monkeypatch):
    configured = registry(tmp_path, monkeypatch)
    configured.remote_enabled = False
    configured.layout.project_mcp.write_text(
        json.dumps(
            {
                "servers": {
                    "failed": {"command": "safe", "allowed_tools": ["read"]},
                    "off": {"enabled": False},
                    "remote": {"transport": "sse", "url": "https://example.test/mcp"},
                }
            }
        )
    )
    manager = McpManager(configured.policy, configured, remote_enabled=False)

    async def fail(name):
        raise RuntimeError("connection unavailable")

    monkeypatch.setattr(manager, "_connect", fail)

    async def scenario():
        await manager.initialize()
        states = {item.name: item for item in manager.statuses()}
        assert not states["failed"].connected
        assert states["failed"].error == "connection unavailable"
        assert states["failed"].allowed_tools == ("read",)
        assert not states["off"].enabled
        assert states["off"].error is None
        assert "disabled" in states["remote"].error
        assert not states["remote"].available_tools
        await manager.close()

    asyncio.run(scenario())


def test_mcp_status_uses_reconnected_server_metadata(tmp_path, monkeypatch):
    configured = registry(tmp_path, monkeypatch)
    configured.layout.project_mcp.write_text('{"servers":{"srv":{"enabled":false}}}')
    manager = McpManager(configured.policy, configured)

    async def scenario():
        await manager.initialize()
        configured.layout.project_mcp.write_text(
            '{"servers":{"srv":{"command":"safe","allowed_tools":["new"]}}}'
        )
        server = configured.get("srv")
        tool = ManagedMcpTool("srv", "new", "new tool", {"type": "object"}, None, {})
        manager._sessions["srv"] = SimpleNamespace(
            server=server, tools=(tool,), resources=(), stack=AsyncExitStack()
        )
        state = manager.statuses()[0]
        assert state.enabled and state.connected
        assert state.allowed_tools == ("new",)
        assert state.available_tools == ("new",)
        await manager.close()

    asyncio.run(scenario())
