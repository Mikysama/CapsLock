from __future__ import annotations

import json
import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from capslock.cli.app import async_main
from capslock.layout import ProjectLayout, UserLayout
from capslock.domain import ActionType
from capslock.plugins import PluginProcessClient, PluginRegistry, PluginService
from capslock.plugins.manifest import PluginValidationError, load_plugin_manifest
from capslock.tooling.plugins import plugin_tools


def _layout(tmp_path: Path) -> ProjectLayout:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ProjectLayout.discover(
        workspace, user=UserLayout((tmp_path / "home").resolve())
    )


def _plugin(tmp_path: Path, *, version: str = "1.0.0", read_access: bool = False) -> Path:
    root = tmp_path / f"source-{version}"
    root.mkdir()
    read_paths = '["**"]' if read_access else "[]"
    (root / "capslock-plugin.toml").write_text(
        f'''manifest_version = 3
protocol_version = 3
name = "echo-plugin"
version = "{version}"
description = "Echo arguments"
entrypoint = ["plugin.py"]

[capabilities]
workspace_read = {read_paths}
workspace_write = []
network_hosts = []
process_templates = []
credentials = []

[[tools]]
name = "echo"
description = "Echo arguments"
parameters = {{type = "object", additionalProperties = true}}
''',
        encoding="utf-8",
    )
    (root / "plugin.py").write_text(
        """import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "initialize":
        result = {"protocol_version": 3}
    elif method == "list_tools":
        result = {"tools": [{"name": "echo"}]}
    elif method == "call_tool":
        result = {"ok": True, "data": request["params"]["arguments"]}
    else:
        raise RuntimeError(method)
    print(json.dumps({"protocol_version": 3, "id": request["id"], "ok": True, "result": result}), flush=True)
""",
        encoding="utf-8",
    )
    return root


def test_manifest_rejects_symlinks(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    (root / "linked").symlink_to(root / "plugin.py")
    with pytest.raises(PluginValidationError, match="regular files|symbolic links"):
        load_plugin_manifest(root)


def test_plugin_protocol_verifies_and_calls(tmp_path: Path) -> None:
    manifest = load_plugin_manifest(_plugin(tmp_path))
    client = PluginProcessClient(timeout_seconds=2)
    asyncio.run(client.verify(manifest, trusted_native=True))
    assert asyncio.run(
        client.call(manifest, "echo", {"value": 3}, trusted_native=True)
    ) == {
        "ok": True,
        "data": {"value": 3},
    }


def test_install_enable_and_uninstall_lifecycle(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    service = PluginService(layout, client=PluginProcessClient(timeout_seconds=2))
    installed = asyncio.run(
        service.install(_plugin(tmp_path, read_access=True))
    )
    assert installed.name == "echo-plugin"
    assert not service.entries()[0].enabled

    service.enable("echo-plugin", trusted_native=True)
    entry = service.entries()[0]
    assert entry.enabled
    assert [tool.name for tool in plugin_tools(PluginRegistry(layout))] == [
        "plugin_echo_plugin_echo"
    ]

    with pytest.raises(PluginValidationError, match="still enabled"):
        service.uninstall("echo-plugin")
    service.disable("echo-plugin")
    service.uninstall("echo-plugin")
    assert service.entries() == []
    audit = [
        json.loads(line)
        for line in layout.user.plugin_audit.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["operation"] for item in audit] == [
        "install",
        "enable",
        "disable",
        "uninstall",
    ]


def test_upgrade_requires_workspace_reenable(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    service = PluginService(layout, client=PluginProcessClient(timeout_seconds=2))
    asyncio.run(service.install(_plugin(tmp_path, version="1.0.0")))
    service.enable("echo-plugin", trusted_native=True)
    asyncio.run(service.install(_plugin(tmp_path, version="1.1.0")))
    assert service.entries()[0].manifest.version == "1.1.0"
    assert not service.entries()[0].enabled
    assert service.registry.enabled_workspaces("echo-plugin") == (
        str(layout.workspace),
    )


def test_plugin_tool_uses_audited_external_action(tmp_path: Path) -> None:
    class Actions:
        def __init__(self) -> None:
            self.calls = []

        async def propose(self, action_type, **arguments):
            self.calls.append((action_type, arguments))
            return type(
                "Action",
                (),
                {
                    "id": "action",
                    "type": action_type,
                    "summary": "plugin",
                    "status": type("Status", (), {"value": "pending"})(),
                    "result_kind": None,
                    "request": arguments,
                    "result": None,
                    "error_message": None,
                },
            )()

    async def scenario() -> None:
        layout = _layout(tmp_path)
        service = PluginService(layout, client=PluginProcessClient(timeout_seconds=2))
        await service.install(_plugin(tmp_path))
        service.enable("echo-plugin", trusted_native=True)
        actions = Actions()
        tool = plugin_tools(PluginRegistry(layout))[0]
        context = type("Context", (), {"actions": actions})()
        result = await tool.execute(context, {"value": 4})
        assert result.ok
        assert actions.calls == [
            (
                ActionType.MCP_CALL,
                {
                    "plugin": "echo-plugin",
                    "tool": "echo",
                    "arguments": {"value": 4},
                },
            )
        ]

    asyncio.run(scenario())


def test_cli_install_requires_explicit_noninteractive_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    layout = _layout(tmp_path)
    monkeypatch.setenv("CAPSLOCK_HOME", str(layout.user.home))
    source = _plugin(tmp_path)
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    assert (
        asyncio.run(
            async_main(
                [
                    "--workspace",
                    str(layout.workspace),
                    "plugin",
                    "install",
                    str(source),
                ],
                console=console,
            )
        )
        == 3
    )
    assert not layout.user.plugin_registry.exists()
    assert (
        asyncio.run(
            async_main(
                [
                    "--workspace",
                    str(layout.workspace),
                    "plugin",
                    "install",
                    str(source),
                    "--yes",
                ],
                console=console,
            )
        )
        == 0
    )
