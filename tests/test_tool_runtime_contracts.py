"""Tool runtime contracts, permissions, discovery, classifier, and config upgrades."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
    PermissionDestination,
    PermissionEngine,
    PermissionUpdate,
    PermissionUpdateOperation,
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


def test_invalid_permission_rule_disables_automatic_grants(tmp_path: Path) -> None:
    permissions = tmp_path / "permissions.toml"
    permissions.write_text(
        """permissions_version = 2
[[rules]]
behavior = "allow"
tool = "read_file"
[rules.constraints]
unknown = true
""",
        encoding="utf-8",
    )

    async def execute(context, arguments):
        return ToolOutcome.success({})

    tool = define_tool(
        "read_file", "Read.", {"type": "object"}, execute,
        policy=ResolvedToolPolicy.safe_read(),
    )
    engine = PermissionEngine((("local", permissions),), object())
    decision = asyncio.run(
        engine.decide(
            tool,
            {},
            ResolvedToolPolicy.safe_read(),
            _context(tmp_path, permission_mode=PermissionMode.FULL_ACCESS),
        )
    )
    assert decision.behavior is PermissionBehavior.ASK
    assert decision.rule is not None and decision.rule.diagnostic
    assert engine.diagnostics()


def test_file_globs_are_anchored_and_double_star_crosses_directories(
    tmp_path: Path,
) -> None:
    permissions = tmp_path / "permissions.toml"
    permissions.write_text(
        """permissions_version = 2
[[rules]]
behavior = "allow"
tool = "read_file"
[rules.constraints]
path = "src/*.py"
""",
        encoding="utf-8",
    )

    async def execute(context, arguments):
        return ToolOutcome.success({})

    tool = define_tool(
        "read_file", "Read.", {"type": "object"}, execute,
        policy=ResolvedToolPolicy.safe_read(),
    )
    engine = PermissionEngine((("local", permissions),), object())
    context = _context(tmp_path, permission_mode=PermissionMode.ASK_FOR_APPROVAL)

    async def decide(path: str):
        arguments = engine.normalize(tool, {"path": path}, context)
        return await engine.decide(
            tool, arguments, ResolvedToolPolicy.safe_read(), context
        )

    assert asyncio.run(decide("src/main.py")).behavior is PermissionBehavior.ALLOW
    assert asyncio.run(decide("src/pkg/main.py")).behavior is PermissionBehavior.ASK
    permissions.write_text(
        permissions.read_text(encoding="utf-8").replace("src/*.py", "src/**/*.py"),
        encoding="utf-8",
    )
    assert asyncio.run(decide("src/main.py")).behavior is PermissionBehavior.ALLOW
    assert asyncio.run(decide("src/pkg/main.py")).behavior is PermissionBehavior.ALLOW


def test_shell_classifier_and_safe_default_cannot_bypass_mode_or_rules(
    tmp_path: Path,
) -> None:
    permissions = tmp_path / "permissions.toml"
    permissions.write_text(
        """permissions_version = 2
[[rules]]
behavior = "ask"
tool = "shell"
[rules.constraints]
command = "custom-build"
""",
        encoding="utf-8",
    )

    async def execute(context, arguments):
        return ToolOutcome.success({})

    tool = define_tool("shell", "Shell.", {"type": "object"}, execute)
    engine = PermissionEngine((("project", permissions),), object())

    def context(mode: PermissionMode) -> ExecutionContext:
        value = _context(tmp_path, permission_mode=mode)
        value.runtime_state.update(
            {
                "classifier_auto_allow": True,
                "shell_classifier": {
                    "honored": True,
                    "confidence": 0.99,
                    "result": "allow",
                },
                "shell_deterministic_behavior": "allow",
            }
        )
        return value

    arguments = {
        "command": "custom-build",
        "cwd": ".",
        "sandbox": "default",
        "network": [],
    }
    for mode in PermissionMode:
        decision = asyncio.run(
            engine.decide(tool, arguments, ResolvedToolPolicy(), context(mode))
        )
        assert decision.behavior is PermissionBehavior.ASK
        assert decision.reason_code == "explicit_ask"

    no_rules = PermissionEngine((), object())
    assert asyncio.run(
        no_rules.decide(
            tool,
            arguments,
            ResolvedToolPolicy(),
            context(PermissionMode.ASK_FOR_APPROVAL),
        )
    ).behavior is PermissionBehavior.ASK
    assert asyncio.run(
        no_rules.decide(
            tool,
            arguments,
            ResolvedToolPolicy(),
            context(PermissionMode.APPROVE_FOR_ME),
        )
    ).reason_code == "classifier_allow"


def test_shell_allow_rejects_compounds_dynamic_expansion_and_redirection(
    tmp_path: Path,
) -> None:
    permissions = tmp_path / "permissions.toml"
    permissions.write_text(
        """permissions_version = 2
[[rules]]
behavior = "allow"
tool = "shell"
[rules.constraints]
command_prefix = "git status"
""",
        encoding="utf-8",
    )

    async def execute(context, arguments):
        return ToolOutcome.success({})

    tool = define_tool("shell", "Shell.", {"type": "object"}, execute)
    engine = PermissionEngine((("local", permissions),), object())
    context = _context(tmp_path, permission_mode=PermissionMode.ASK_FOR_APPROVAL)

    async def behavior(command: str) -> PermissionBehavior:
        return (
            await engine.decide(
                tool,
                {
                    "command": command,
                    "cwd": ".",
                    "sandbox": "default",
                    "network": [],
                },
                ResolvedToolPolicy(),
                context,
            )
        ).behavior

    assert asyncio.run(behavior("git status --short")) is PermissionBehavior.ALLOW
    assert asyncio.run(behavior("git status && pwd")) is PermissionBehavior.ASK
    assert asyncio.run(behavior("git status $(whoami)")) is PermissionBehavior.ASK
    assert asyncio.run(behavior("git status > output.txt")) is PermissionBehavior.ASK


def test_project_allow_requires_current_digest_trust(tmp_path: Path) -> None:
    class Repository:
        value: str | None = None

        async def permission_setting(self, key):
            return self.value

        async def set_permission_setting(self, key, value):
            self.value = value

    repository = Repository()
    project = tmp_path / "permissions.toml"
    project.write_text(
        'permissions_version=2\n[[rules]]\nbehavior="allow"\ntool="read_file"\n',
        encoding="utf-8",
    )

    async def execute(context, arguments):
        return ToolOutcome.success({})

    tool = define_tool(
        "read_file", "Read.", {"type": "object"}, execute,
        policy=ResolvedToolPolicy.safe_read(),
    )
    engine = PermissionEngine((("project", project),), repository)
    context = _context(tmp_path, permission_mode=PermissionMode.ASK_FOR_APPROVAL)

    async def decide():
        return await engine.decide(
            tool, {}, ResolvedToolPolicy.safe_read(), context
        )

    assert asyncio.run(decide()).reason_code == "project_allow_untrusted"
    asyncio.run(engine.trust_project_permissions())
    assert asyncio.run(decide()).behavior is PermissionBehavior.ALLOW
    project.write_text(project.read_text() + "\n# changed\n", encoding="utf-8")
    assert asyncio.run(decide()).reason_code == "project_allow_untrusted"


def test_mcp_rules_match_exact_server_and_tool(tmp_path: Path) -> None:
    permissions = tmp_path / "permissions.toml"
    permissions.write_text(
        """permissions_version = 2
[[rules]]
behavior = "allow"
tool = "mcp__docs__lookup"
[rules.constraints]
server = "docs"
mcp_tool = "lookup"
""",
        encoding="utf-8",
    )

    async def execute(context, arguments):
        return ToolOutcome.success({})

    allowed = define_tool("mcp__docs__lookup", "MCP.", {"type": "object"}, execute)
    other = define_tool("mcp__docs__write", "MCP.", {"type": "object"}, execute)
    engine = PermissionEngine((("local", permissions),), object())
    context = _context(tmp_path, permission_mode=PermissionMode.ASK_FOR_APPROVAL)
    assert asyncio.run(
        engine.decide(allowed, {}, ResolvedToolPolicy(), context)
    ).behavior is PermissionBehavior.ALLOW
    assert asyncio.run(
        engine.decide(other, {}, ResolvedToolPolicy(), context)
    ).behavior is PermissionBehavior.ASK


def test_local_permission_updates_are_atomic_preserve_unknown_toml_and_reject_symlink(
    tmp_path: Path,
) -> None:
    local = tmp_path / ".capslock" / "local" / "permissions.toml"
    local.parent.mkdir(parents=True)
    local.write_text('owner = "keep"\npermissions_version = 2\n', encoding="utf-8")
    engine = PermissionEngine((("local", local),), object())
    update = PermissionUpdate(
        PermissionUpdateOperation.ADD,
        PermissionDestination.LOCAL,
        PermissionBehavior.ALLOW,
        "read_file",
        {"path": "src/**/*.py"},
    )
    identifier = asyncio.run(engine.apply_update("session", update))
    contents = local.read_text(encoding="utf-8")
    assert 'owner = "keep"' in contents
    assert identifier in contents
    assert not list(local.parent.glob("*.tmp"))

    target = tmp_path / "target.toml"
    local.unlink()
    target.write_text("permissions_version = 2\n", encoding="utf-8")
    local.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        asyncio.run(engine.apply_update("session", update))


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
    assert document["config_version"] == 6
    assert document["tools"]["schema_budget_tokens"] == 8000
    assert document["shell"]["classifier_threshold"] == 0.95
    backups = list(tmp_path.glob("config.toml.*.bak"))
    assert len(backups) == 1
    assert "config_version = 3" in backups[0].read_text(encoding="utf-8")
