"""Focused acceptance tests for priority-one and priority-two tools."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.application.action_system import (
    ActionCoordinator,
    ActionRunState,
    FileActionHandler,
)
from capslock.configuration import LspServerSettings, LspSettings
from capslock.domain import ActionType
from capslock.lsp import LspManager
from capslock.permissions import PermissionMode
from capslock.policy import WorkspacePolicy
from capslock.storage.repositories import WorkspaceRepositories
from capslock.tooling.contracts import ExecutionContext, ToolOutcome
from capslock.tooling.tools import workspace_tools
from capslock.tooling.tools.documents import edit_notebook, read_notebook
from capslock.tooling.tools.filesystem import glob_files, read_image, write_file
from capslock.tooling.tools.lsp import lsp_tools
from capslock.tooling.tools.mcp import mcp_resource_tools
from tests.helpers import StubActionHandler, workspace_run


def _context(
    root: Path,
    *,
    actions: object,
    tasks: object | None = None,
    artifacts: object | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        session_id="session",
        run_id="run",
        invocation_id="invocation",
        policy=WorkspacePolicy(root),
        event=lambda *args, **kwargs: None,
        actions=actions,
        tasks=tasks,
        artifacts=artifacts,
        permission_mode=PermissionMode.FULL_ACCESS,
    )


def test_priority_catalog_uses_only_direct_capability_names() -> None:
    names = workspace_tools().names
    assert {
        "write_file",
        "glob_files",
        "ask_user",
        "create_task",
        "list_tasks",
        "get_task",
        "update_task",
        "read_pdf",
        "read_notebook",
        "edit_notebook",
        "create_worktree",
        "exit_worktree",
        "get_agent_task",
        "stop_agent_task",
    } <= names
    assert not any(name.startswith("propose_") for name in names)
    assert "task_list_update" not in names
    assert "task_status_update" not in names


def test_glob_python_fallback_honors_gitignore_and_reads_binary_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "hidden.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "top.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("ignored\n", encoding="utf-8")
    monkeypatch.setattr(
        "capslock.tooling.tools.filesystem.shutil.which", lambda name: None
    )

    async def scenario() -> None:
        context = _context(tmp_path, actions=object())
        result = await glob_files(context, {"pattern": "**/*.py"})
        assert result.data["files"] == ["top.py"]
        (tmp_path / "pixel.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        image = await read_image(context, {"path": "pixel.png"})
        assert image.ok
        assert image.data["media_type"] == "image/png"
        assert image.content[0].kind == "image"

    asyncio.run(scenario())


def test_write_file_hash_contract_and_notebook_revalidation(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "tools.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repositories)
            handlers = [FileActionHandler(WorkspacePolicy(tmp_path))]
            handlers.append(StubActionHandler(set(ActionType) - set(handlers[0].types)))
            actions = ActionCoordinator(
                repositories.actions,
                ActionRunState(repositories.runs, repositories.workflow),
                session_id=session.id,
                run_id=prepared.run.id,
                handlers=handlers,
                event=lambda *args, **kwargs: None,
                permission_mode=PermissionMode.FULL_ACCESS,
            )
            context = ExecutionContext(
                session_id=session.id,
                run_id=prepared.run.id,
                policy=WorkspacePolicy(tmp_path),
                event=lambda *args, **kwargs: None,
                actions=actions,
            )
            created = await write_file(
                context,
                {"path": "value.txt", "content": "one\n", "expected_sha256": None},
            )
            assert isinstance(created, ToolOutcome) and created.ok
            digest = hashlib.sha256(b"one\n").hexdigest()
            changed = await write_file(
                context,
                {
                    "path": "value.txt",
                    "content": "two\n",
                    "expected_sha256": digest,
                },
            )
            assert isinstance(changed, ToolOutcome) and changed.ok
            with pytest.raises(ValueError, match="hash"):
                await write_file(
                    context,
                    {
                        "path": "value.txt",
                        "content": "three\n",
                        "expected_sha256": digest,
                    },
                )

            notebook = {
                "cells": [
                    {
                        "id": "cell-a",
                        "cell_type": "code",
                        "metadata": {},
                        "source": ["print(1)\n"],
                        "outputs": [],
                        "execution_count": None,
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
            raw = (json.dumps(notebook) + "\n").encode()
            (tmp_path / "demo.ipynb").write_bytes(raw)
            read = await read_notebook(context, {"path": "demo.ipynb"})
            assert read.data["total_cells"] == 1
            paused_or_done = await edit_notebook(
                context,
                {
                    "path": "demo.ipynb",
                    "mode": "replace",
                    "cell_id": "cell-a",
                    "source": "print(2)\n",
                    "expected_sha256": hashlib.sha256(raw).hexdigest(),
                },
            )
            assert isinstance(paused_or_done, ToolOutcome) and paused_or_done.ok
            assert "print(2)" in (tmp_path / "demo.ipynb").read_text()
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_task_dependencies_reject_cycles_and_cross_session_ids(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "tasks.sqlite3", workspace=tmp_path
        )
        try:
            first_session = await repositories.sessions.create("model")
            other_session = await repositories.sessions.create("model")
            first = await repositories.tasks.create(first_session.id, subject="First")
            second = await repositories.tasks.create(
                first_session.id, subject="Second", blocked_by=[first.id]
            )
            with pytest.raises(ValueError, match="cycle"):
                await repositories.tasks.update(
                    first.id, first_session.id, blocked_by=[second.id]
                )
            foreign = await repositories.tasks.create(
                other_session.id, subject="Foreign"
            )
            with pytest.raises(ValueError, match="outside this session"):
                await repositories.tasks.update(
                    first.id, first_session.id, blocked_by=[foreign.id]
                )
            assert (
                await repositories.tasks.get(foreign.id, session_id=first_session.id)
                is None
            )
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_lsp_stdio_open_change_limit_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake_lsp.py"
    log = tmp_path / "notifications.jsonl"
    fake.write_text(
        """import json, sys
log, default_uri = sys.argv[1:3]
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise SystemExit
        if line in (b'\\n', b'\\r\\n'):
            break
        key, value = line.decode().split(':', 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
    if 'id' not in message:
        with open(log, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(message) + '\\n')
        if message.get('method') == 'exit':
            raise SystemExit
        continue
    method = message.get('method')
    if method == 'shutdown':
        result = None
    elif method == 'initialize':
        result = {'capabilities': {'textDocumentSync': 1}}
    else:
        uri = message.get('params', {}).get('textDocument', {}).get('uri', default_uri)
        result = [
            {'uri': uri, 'range': {'start': {'line': i, 'character': 0},
             'end': {'line': i, 'character': 1}}} for i in range(3)
        ]
    body = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode()
    sys.stdout.buffer.write(b'Content-Length: ' + str(len(body)).encode() + b'\\r\\n\\r\\n' + body)
    sys.stdout.buffer.flush()
""",
        encoding="utf-8",
    )
    source = tmp_path / "sample.py"
    source.write_text("a\nb\nc\n", encoding="utf-8")
    settings = LspSettings(
        servers={
            "python": LspServerSettings(
                (sys.executable, str(fake), str(log), source.as_uri()), (".py",), ()
            )
        }
    )
    runtime = LspManager(WorkspacePolicy(tmp_path), settings)
    monkeypatch.setattr(
        "capslock.lsp.manager.sandboxed_lsp_command",
        lambda command, root: command,
    )

    async def scenario() -> None:
        tool = next(
            item for item in lsp_tools(runtime) if item.name == "search_symbols"
        )
        outcome = await tool.execute(
            _context(tmp_path, actions=object()),
            {"path": "sample.py", "query": "a", "limit": 2},
            lambda event: asyncio.sleep(0),
        )
        assert isinstance(outcome, ToolOutcome)
        assert outcome.data["count"] == 2
        assert outcome.data["total_count"] == 3
        assert outcome.data["truncated"] is True
        assert all(
            item["evidence_id"].startswith("lsp_") for item in outcome.data["locations"]
        )
        source.write_text("changed\n", encoding="utf-8")
        await runtime.did_change("sample.py")
        await runtime.close()

    asyncio.run(scenario())
    notifications = [json.loads(line) for line in log.read_text().splitlines()]
    assert (
        sum(item.get("method") == "textDocument/didOpen" for item in notifications) == 1
    )
    changes = [
        item for item in notifications if item.get("method") == "textDocument/didChange"
    ]
    assert changes[0]["params"]["textDocument"]["version"] == 2
    assert changes[0]["params"]["contentChanges"] == [{"text": "changed\n"}]


def test_mcp_binary_resource_uses_artifact_block(tmp_path: Path) -> None:
    class Manager:
        errors = {}

        def resources(self, server=None):
            return ()

        async def read_resource(self, server, uri):
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/octet-stream",
                        "blob": base64.b64encode(b"binary").decode(),
                    }
                ]
            }

    class Artifacts:
        async def put(self, **values):
            assert values["content"] == b"binary"
            return SimpleNamespace(
                id="artifact-1",
                sha256=hashlib.sha256(b"binary").hexdigest(),
                size_bytes=6,
            )

    async def scenario() -> None:
        tool = next(
            item
            for item in mcp_resource_tools(Manager())
            if item.name == "read_mcp_resource"
        )
        result = await tool.execute(
            _context(tmp_path, actions=object(), artifacts=Artifacts()),
            {"server": "demo", "uri": "data://blob"},
            lambda event: asyncio.sleep(0),
        )
        assert isinstance(result, ToolOutcome) and result.ok
        assert result.content[0].kind == "artifact"
        assert result.data["contents"][0]["artifact_id"] == "artifact-1"

    asyncio.run(scenario())
