"""Tool runtime contracts, permissions, discovery, classifier, and config upgrades."""

from __future__ import annotations

import asyncio
from pathlib import Path

from capslock.configuration.loader import load_config_document
from capslock.permissions import PermissionMode
from capslock.policy import WorkspacePolicy
from capslock.runtime.model import ModelMessage, ModelResponse, ModelUsage
from capslock.shell import ModelShellClassifier, assess_shell
from capslock.tooling.catalog import ToolCatalog
from capslock.tooling.contracts import (
    ExecutionContext,
    ResolvedToolPolicy,
    ToolOutcome,
    ToolOutcomeStatus,
    InterruptBehavior,
    define_tool,
)
from capslock.tooling.executor import ToolRuntime
from capslock.tooling.authorization import (
    PermissionBehavior,
    PermissionEngine,
)


def _context(tmp_path: Path, **values) -> ExecutionContext:
    return ExecutionContext(
        session_id="session",
        run_id="run",
        policy=WorkspacePolicy(tmp_path),
        event=lambda *args, **kwargs: None,
        actions=object(),
        **values,
    )


def test_invalid_output_preserves_execution_truth(tmp_path: Path) -> None:
    async def execute(context, arguments):
        return ToolOutcome.success({"changed": True})

    tool = define_tool(
        "mutate",
        "Mutate something.",
        {"type": "object"},
        execute,
        output_schema={"type": "string"},
    )
    result = asyncio.run(ToolRuntime([tool]).invoke("mutate", _context(tmp_path), {}))
    assert result.outcome.status is ToolOutcomeStatus.FAILED
    assert result.outcome.executed
    assert result.outcome.error_code == "invalid_tool_output"


def test_complete_interrupt_finishes_side_effect_before_return(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        finished = asyncio.Event()

        async def execute(context, arguments):
            started.set()
            await asyncio.sleep(0.02)
            finished.set()
            return ToolOutcome.success({"written": True})

        tool = define_tool(
            "write",
            "Write data.",
            {"type": "object"},
            execute,
            policy=ResolvedToolPolicy(
                external_side_effects=True,
                interrupt_behavior=InterruptBehavior.COMPLETE,
            ),
        )
        context = _context(tmp_path)
        invocation = asyncio.create_task(
            ToolRuntime([tool]).invoke("write", context, {})
        )
        await started.wait()
        invocation.cancel()
        result = await invocation
        assert finished.is_set()
        assert result.outcome.ok and result.outcome.executed
        assert context.runtime_state["interrupt_pending"] is True

    asyncio.run(scenario())


def test_catalog_fingerprint_and_deferred_discovery_are_stable() -> None:
    async def execute(context, arguments):
        return ToolOutcome.success(arguments)

    core = define_tool("core", "Core tool.", {"type": "object"}, execute)
    deferred = define_tool(
        "plugin__demo__lookup",
        "Look up a demo value.",
        {"type": "object"},
        execute,
        deferred=True,
        search_hint="demo lookup",
    )
    catalog = ToolCatalog([deferred, core])
    initial = catalog.snapshot()
    assert [item.name for item in initial.tools] == ["core"]
    assert catalog.snapshot().fingerprint == initial.fingerprint
    assert catalog.search("demo") == ("plugin__demo__lookup",)
    discovered = catalog.snapshot()
    assert [item.name for item in discovered.tools] == ["core", "plugin__demo__lookup"]
    assert discovered.fingerprint != initial.fingerprint


def test_permission_precedence_ask_beats_allow(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "project.toml"
    user.write_text('[[rules]]\nbehavior="allow"\ntool="read_file"\n')
    project.write_text('[[rules]]\nbehavior="ask"\ntool="read_file"\n')

    async def execute(context, arguments):
        return ToolOutcome.success({})

    tool = define_tool(
        "read_file",
        "Read a file.",
        {"type": "object"},
        execute,
        policy=ResolvedToolPolicy.safe_read(),
    )
    context = _context(tmp_path, permission_mode=PermissionMode.FULL_ACCESS)
    engine = PermissionEngine((("user", user), ("project", project)), object())
    decision = asyncio.run(
        engine.decide(tool, {}, ResolvedToolPolicy.safe_read(), context)
    )
    assert decision.behavior is PermissionBehavior.ASK
    assert decision.source == "project"


def test_shell_deterministic_hard_denies_and_model_threshold() -> None:
    assert assess_shell("git status").behavior == "allow"
    assert assess_shell("sudo rm -rf /").behavior == "deny"
    assert assess_shell("rm -rf $TARGET").behavior == "deny"
    assert assess_shell("echo $(whoami)").behavior == "ask"

    class Model:
        async def complete(self, **values):
            return ModelResponse(
                ModelMessage(
                    '{"behavior":"allow","confidence":0.94,"reason":"looks safe"}'
                ),
                ModelUsage(9, 4),
            )

    classifier = ModelShellClassifier(Model(), model_name="fast", threshold=0.95)
    result = asyncio.run(
        classifier.classify(
            command="custom-build", cwd=".", sandbox="default", parsed=("custom-build",)
        )
    )
    assert result.behavior == "ask"
    assert result.audit["honored"] is False
    assert result.audit["input_tokens"] == 9


def test_config_document_is_backed_up_and_upgraded_atomically(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """config_version = 3
[providers.main]
kind = "openai_compatible"
base_url = "https://example.invalid"
credential = "env:CAPSLOCK_TEST_KEY"
[models.main]
provider = "main"
model = "test"
[routing]
reasoning = ["main"]
""",
        encoding="utf-8",
    )
    document = load_config_document(path)
    assert document["config_version"] == 5
    assert document["tools"]["schema_budget_tokens"] == 8000
    assert document["shell"]["classifier_threshold"] == 0.95
    backups = list(tmp_path.glob("config.toml.*.bak"))
    assert len(backups) == 1
    assert "config_version = 3" in backups[0].read_text(encoding="utf-8")
