"""Regression contracts for the prioritized optimization modules."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.bridge import IdeBridgeServer
from capslock.collaboration import MailboxMessageKind
from capslock.collaboration.models import AgentTaskContract
from capslock.configuration import ContextSettings
from capslock.mcp.registry import McpRegistry
from capslock.policy import PolicyError, WorkspacePolicy
from capslock.runtime.attachments import LocalAttachmentResolver
from capslock.runtime.context import ContextBudgetManager
from capslock.runtime.tokens import AdaptiveTokenEstimator
from capslock.shell import assess_shell, parse_shell
from capslock.storage.repositories import WorkspaceRepositories
from capslock.tooling.tools.collaboration import child_mailbox_tools
from capslock.layout import ProjectLayout
from tests.helpers import workspace_run


class _NoCompactions:
    async def active(self, _session):
        return None


def test_shell_ast_is_fail_closed_for_dynamic_and_redirected_commands() -> None:
    syntax = parse_shell("git status && rg TODO | head -10")
    assert syntax.parser_available and syntax.commands == ("git", "rg", "head")
    assert [segment.argv for segment in syntax.segments] == [
        ("git", "status"),
        ("rg", "TODO"),
        ("head", "-10"),
    ]
    assert assess_shell("git status && rg TODO | head -10").behavior == "ask"
    safe = assess_shell("git status && git diff | head -10")
    assert safe.behavior == "allow" and safe.read_only_workspace
    assert assess_shell("git status > out.txt").behavior == "ask"
    assert assess_shell("echo $(whoami)").behavior == "ask"
    assert assess_shell("echo $HOME").behavior == "ask"
    assert assess_shell("git status &").behavior == "ask"


def test_child_agents_receive_contract_bound_mailbox_tools() -> None:
    service = type("MailboxService", (), {"mailbox_enabled": True})()
    contract = AgentTaskContract.create("run-parent", "inspect")
    assert {tool.name for tool in child_mailbox_tools(service, contract)} == {
        "read_parent_messages",
        "send_parent_message",
        "ack_parent_message",
    }


def test_bridge_context_requires_auth_and_only_explicit_mentions_expand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeServer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def fake_start_unix_server(_handler, *, path):
        Path(path).touch()
        return FakeServer()

    monkeypatch.setattr(asyncio, "start_unix_server", fake_start_unix_server)

    async def scenario() -> None:
        (tmp_path / "a.py").write_text("one\ntwo\n", encoding="utf-8")
        bridge = IdeBridgeServer(tmp_path, tmp_path / ".capslock" / "state")
        await bridge.start()
        try:
            with pytest.raises(PermissionError):
                bridge.handle(
                    {"jsonrpc": "2.0", "id": 1, "token": "bad", "method": "initialize"}
                )
            bridge.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "token": bridge.token,
                    "method": "editor/context",
                    "params": {
                        "active_file": "a.py",
                        "selection": {
                            "path": "a.py",
                            "text": "two",
                            "start_line": 2,
                            "end_line": 2,
                        },
                        "diagnostics": [
                            {"path": "a.py", "line": 2, "message": "example"}
                        ],
                    },
                }
            )
            resolver = LocalAttachmentResolver(WorkspacePolicy(tmp_path), bridge=bridge)
            assert "ide-selection" not in resolver.expand("review this")
            assert "two" in resolver.expand("review @selection")
            assert "example" in resolver.expand("review @diagnostics")
            assert bridge.descriptor_path.stat().st_mode & 0o777 == 0o600
        finally:
            await bridge.close()

    asyncio.run(scenario())


def test_adaptive_token_calibration_and_micro_compaction() -> None:
    class Sessions:
        async def context_entries(self, *_args, **_kwargs):
            return []

    estimator = AdaptiveTokenEstimator("p", safety_margin=1.15)
    before = estimator.estimate({"text": "中文 code " * 20})
    asyncio.run(estimator.observe({"text": "中文 code " * 20}, before * 2))
    assert estimator.samples == 1 and estimator.ratio > 1

    class Artifacts:
        async def put(self, **values):
            content = values["content"]
            return SimpleNamespace(
                id="artifact-test",
                sha256=hashlib.sha256(content).hexdigest(),
                preview=content[:32].decode(),
            )

    manager = ContextBudgetManager(
        sessions=Sessions(),
        compactions=_NoCompactions(),
        settings=ContextSettings(),
        context_window=10_000,
        max_output_tokens=100,
        model_profile="p",
        model_name="m",
        tool_schemas=[],
        artifacts=Artifacts(),
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "tool", "tool_call_id": "x", "content": "x" * 20_000},
        *({"role": "user", "content": str(i)} for i in range(20)),
    ]
    compacted, saved = asyncio.run(
        manager.micro_compact(messages, session_id="session", run_id="run")
    )
    assert saved > 0
    assert compacted[1]["tool_call_id"] == "x"
    assert "artifact-test" in str(compacted[1]["content"])


def test_performance_spans_and_mailbox_are_digest_checked(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            span_id = await repositories.performance.record(
                trace_id="trace",
                session_id=session.id,
                run_id=prepared.run.id,
                category="model",
                name="ttfb",
                duration_ms=12.5,
                attributes={"api_key": "secret", "tokens": 7},
            )
            span = (await repositories.performance.trace("trace"))[0]
            assert span["id"] == span_id
            assert span["attributes"]["api_key"] == "<redacted>"
            contract = AgentTaskContract.create(prepared.run.id, "inspect")
            await repositories.collaboration.create_task(contract)
            message = await repositories.collaboration.send_mailbox(
                task_id=contract.task_id,
                parent_run_id=prepared.run.id,
                sender="child",
                recipient="parent",
                kind=MailboxMessageKind.QUESTION,
                payload={"question": "which file?"},
            )
            delivered = await repositories.collaboration.receive_mailbox(
                contract.task_id, recipient="parent"
            )
            assert delivered[0]["status"] == "delivered"
            await repositories.collaboration.acknowledge_mailbox(
                message["id"], recipient="parent"
            )
            assert (await repositories.collaboration.mailbox_message(message["id"]))[
                "status"
            ] == "acknowledged"
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_remote_mcp_config_requires_https_public_and_private_headers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout.discover(tmp_path)
    layout.project_mcp.parent.mkdir(parents=True)
    layout.project_mcp.write_text(
        json.dumps(
            {
                "servers": {
                    "remote": {
                        "transport": "streamable_http",
                        "url": "http://example.com/mcp",
                        "allowed_tools": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="HTTPS"):
        McpRegistry(WorkspacePolicy(tmp_path), layout=layout).servers()
    layout.project_mcp.write_text(
        json.dumps(
            {
                "servers": {
                    "remote": {
                        "transport": "streamable_http",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "secret"},
                        "allowed_tools": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="headers"):
        McpRegistry(WorkspacePolicy(tmp_path), layout=layout).servers()
