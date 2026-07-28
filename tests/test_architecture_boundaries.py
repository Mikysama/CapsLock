"""Regression tests for dependency directions introduced by refactors."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, get_type_hints

from capslock.runtime.agent import AgentSession
from capslock.runtime.session_services import (
    PermissionRequestService,
    PlanRequestService,
    RunExecutionCoordinator,
    SessionAdministration,
)
from capslock.runtime.tool_delivery import BatchScheduler, ResultDelivery
from capslock.runtime.tool_invocation import InvocationPreparer
from capslock.runtime.tool_loop import ToolCallExecutor
from capslock.storage.repositories.journal.events import RunEventJournalRepository
from capslock.storage.repositories.journal.input_requests import (
    InputRequestJournalRepository,
)
from capslock.storage.repositories.journal.permissions import (
    PermissionJournalRepository,
)
from capslock.storage.repositories.journal.repository import RunJournalRepository
from capslock.storage.repositories.journal.tool_invocations import (
    ToolInvocationJournalRepository,
)
from capslock.tooling.permission_policy.engine import PermissionEngine
from capslock.tooling.permission_policy.middleware import (
    PermissionMiddleware as SplitPermissionMiddleware,
)
from capslock.tooling.permission_policy.models import (
    PermissionDecision,
)
from capslock.tooling.contracts import ExecutionContext


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add("." * node.level + (node.module or ""))
    return result


def test_cli_input_requests_does_not_depend_on_app() -> None:
    root = Path(__file__).parents[1]
    imports = _imports(root / "capslock" / "cli" / "input_requests.py")
    assert ".app" not in imports
    assert "capslock.cli.app" not in imports


def test_cli_factory_does_not_depend_on_dispatch_modules() -> None:
    root = Path(__file__).parents[1]
    imports = _imports(root / "capslock" / "cli" / "factory.py")
    assert not imports & {".app", ".input_requests", "capslock.cli.app"}


def test_execution_context_service_fields_are_typed_ports() -> None:
    hints = get_type_hints(ExecutionContext)
    for name in (
        "collaboration",
        "governor",
        "artifacts",
        "permission_engine",
        "process_manager",
        "catalog",
        "discoveries",
        "shell_classifier",
        "planning",
    ):
        assert hints[name] is not Any


def test_composite_boundaries_use_split_implementations() -> None:
    assert PermissionDecision is not None
    assert SplitPermissionMiddleware is not None
    assert issubclass(
        RunJournalRepository,
        (
            PermissionJournalRepository,
            ToolInvocationJournalRepository,
            InputRequestJournalRepository,
            RunEventJournalRepository,
        ),
    )
    assert all(
        hasattr(AgentSession, name)
        for name in ("run_stream", "permission_requests", "plan_requests", "rename")
    )
    assert all(
        component is not None
        for component in (
            PermissionEngine,
            PermissionRequestService,
            PlanRequestService,
            RunExecutionCoordinator,
            SessionAdministration,
            InvocationPreparer,
            ResultDelivery,
            BatchScheduler,
            ToolCallExecutor,
        )
    )
