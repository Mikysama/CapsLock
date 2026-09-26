"""Tool runtime contracts, permissions, discovery, classifier, and config upgrades."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.configuration.loader import load_config_document
from capslock.configuration import CONFIG_VERSION
from capslock.permissions import PermissionMode
from capslock.mcp.manager import McpManager
from capslock.ports.mcp import ManagedMcpTool
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
    ToolExecutionState,
    define_tool,
)
from capslock.tooling.executor import ToolRuntime
from capslock.tooling.schema import compile_json_schema
from capslock.tooling.tools import workspace_tools
from capslock.tooling.tools.filesystem.search import search_files
from capslock.tooling.permission_policy.engine import PermissionEngine
from capslock.tooling.permission_policy.middleware import PermissionMiddleware
from capslock.tooling.permission_policy.models import (
    PermissionBehavior,
    PermissionDestination,
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


@pytest.mark.parametrize("use_ripgrep", [True, False])
def test_search_files_enforces_read_limit_with_and_without_ripgrep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_ripgrep: bool
) -> None:
    large = tmp_path / "large.txt"
    large.write_text("needle " + "x" * 200, encoding="utf-8")
    private = tmp_path / ".env"
    private.write_text("needle-secret", encoding="utf-8")
    context = ExecutionContext(
        session_id="session",
        run_id="run",
        policy=WorkspacePolicy(tmp_path, max_file_bytes=16),
        event=lambda *args, **kwargs: None,
        actions=object(),
    )
    if not use_ripgrep:
        monkeypatch.setattr(
            "capslock.tooling.tools.filesystem.search.shutil.which", lambda _: None
        )
    result = asyncio.run(
        search_files(context, {"path": ".", "query": "needle", "limit": 1})
    )
    if use_ripgrep:
        assert result.ok and result.data == []
    else:
        assert not result.ok and result.error_code == "search_backend_unavailable"


@pytest.mark.parametrize("use_ripgrep", [True, False])
def test_search_files_honors_privacy_glob_and_result_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_ripgrep: bool
) -> None:
    (tmp_path / ".env").write_text("needle-secret", encoding="utf-8")
    (tmp_path / "one.txt").write_text("needle-one\nneedle-two", encoding="utf-8")
    (tmp_path / "two.md").write_text("needle-three", encoding="utf-8")
    if not use_ripgrep:
        monkeypatch.setattr(
            "capslock.tooling.tools.filesystem.search.shutil.which", lambda _: None
        )
    result = asyncio.run(
        search_files(
            _context(tmp_path),
            {"path": ".", "query": "needle", "glob": "*.txt", "limit": 1},
        )
    )
    if not use_ripgrep:
        assert not result.ok and result.error_code == "search_backend_unavailable"
        return
    assert result.ok and len(result.data) == 1
    assert result.data[0]["path"].endswith("one.txt")
    assert "secret" not in result.data[0]["text"]


def test_search_files_rejects_symlinked_matches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("needle-secret", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(secret)
    result = asyncio.run(
        search_files(_context(workspace), {"path": ".", "query": "needle"})
    )
    assert result.ok and result.data == []


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


def test_dynamic_tool_with_non_strict_schema_is_quarantined() -> None:
    async def execute(context, arguments):
        return ToolOutcome.success({})

    core = define_tool("core", "Core.", {"type": "object"}, execute)
    incompatible = define_tool(
        "dynamic",
        "Dynamic.",
        {"$ref": "#/$defs/input", "$defs": {"input": {"type": "object"}}},
        execute,
    )
    catalog = ToolCatalog([core])

    catalog.configure_dynamic(lambda: [incompatible])
    asyncio.run(catalog.refresh_dynamic())

    assert catalog.names == {"core"}
    diagnostic = catalog.pop_refresh_diagnostics()[0]
    assert diagnostic["code"] == "strict_schema_incompatible"
    assert diagnostic["tool"] == "dynamic"


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


def test_dynamic_catalog_refresh_keeps_last_known_good_snapshot() -> None:
    async def execute(context, arguments):
        return ToolOutcome.success(arguments)

    core = define_tool("core", "Core tool.", {"type": "object"}, execute)
    runtime = ToolRuntime([core])

    async def broken_provider():
        raise RuntimeError("metadata unavailable")

    runtime.configure_dynamic(broken_provider)
    before = runtime.snapshot()
    after = asyncio.run(runtime.refresh_dynamic())
    assert after.fingerprint == before.fingerprint
    assert runtime.pop_refresh_diagnostics() == (
        {
            "code": "catalog_refresh_failed",
            "error": "metadata unavailable",
            "error_type": "RuntimeError",
        },
    )


def test_side_effect_timeout_reports_unknown_execution(tmp_path: Path) -> None:
    async def execute(context, arguments):
        await asyncio.sleep(0.05)
        return ToolOutcome.success({"changed": True})

    tool = define_tool(
        "mutate",
        "Mutate slowly.",
        {"type": "object"},
        execute,
        policy=ResolvedToolPolicy(
            external_side_effects=True,
            timeout_seconds=0.001,
        ),
    )
    result = asyncio.run(ToolRuntime([tool]).invoke("mutate", _context(tmp_path), {}))
    assert result.outcome.error_code == "unknown_execution"
    assert result.outcome.effective_execution_state is ToolExecutionState.UNKNOWN
    assert result.outcome.executed is False


def test_mcp_stdio_reconnect_retries_only_read_only_tools(tmp_path: Path) -> None:
    class Session:
        def __init__(self, result):
            self.result = result

        async def call_tool(self, name, arguments):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    async def scenario(read_only: bool):
        manager = McpManager(
            WorkspacePolicy(tmp_path),
            SimpleNamespace(),
            timeout_seconds=1,
        )
        spec = ManagedMcpTool(
            "demo",
            "tool",
            "tool",
            {"type": "object"},
            None,
            {"readOnlyHint": read_only},
        )
        first = SimpleNamespace(
            tools=(spec,),
            lock=asyncio.Lock(),
            server=SimpleNamespace(transport="stdio"),
            session=Session(ConnectionError("lost response")),
        )
        second = SimpleNamespace(
            tools=(spec,),
            lock=asyncio.Lock(),
            server=SimpleNamespace(transport="stdio"),
            session=Session({"ok": True}),
        )
        manager._sessions["demo"] = first
        reconnects = 0

        async def reconnect(name):
            nonlocal reconnects
            reconnects += 1
            return second

        manager._reconnect = reconnect
        if read_only:
            assert await manager.call("demo", "tool", {}) == {"ok": True}
            assert reconnects == 1
        else:
            with pytest.raises(ConnectionError, match="lost response"):
                await manager.call("demo", "tool", {})
            assert reconnects == 0

    asyncio.run(scenario(True))
    asyncio.run(scenario(False))


def test_all_builtin_tools_have_output_contracts_and_selection_metadata() -> None:
    runtime = workspace_tools()
    assert len(runtime.names) == 49
    for tool in runtime.catalog._tools.values():
        assert tool.contract.output_schema is not None
        assert tool.contract.intent_tags
        assert tool.contract.tool_group
        compile_json_schema(tool.contract.output_schema)


def test_filesystem_path_mistakes_are_repairable_tool_outcomes(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = workspace_tools()
        context = _context(tmp_path)
        for path in ("frame_transform_graph.transform(FunctionTransform", "missing.py"):
            result = await runtime.invoke("read_file", context, {"path": path})
            assert result.execution.error_code == "invalid_path"
            assert result.execution.data["retryable"] is True

    asyncio.run(scenario())


def test_filtered_tool_selection_keeps_control_fallbacks() -> None:
    runtime = workspace_tools(selection_mode="filtered")
    schemas, candidates = runtime.model_schemas("search source text in files")
    names = [item["function"]["name"] for item in schemas]
    assert names == [name for name in sorted(runtime.names) if name in candidates]
    assert {"ask_user", "search_tools", "search_files"}.issubset(names)
    assert len(names) <= 12


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
        "read_file",
        "Read.",
        {"type": "object"},
        execute,
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
        "read_file",
        "Read.",
        {"type": "object"},
        execute,
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
    assert (
        asyncio.run(
            no_rules.decide(
                tool,
                arguments,
                ResolvedToolPolicy(),
                context(PermissionMode.ASK_FOR_APPROVAL),
            )
        ).behavior
        is PermissionBehavior.ASK
    )
    classifier_cannot_allow = asyncio.run(
        no_rules.decide(
            tool,
            arguments,
            ResolvedToolPolicy(),
            context(PermissionMode.APPROVE_FOR_ME),
        )
    )
    assert classifier_cannot_allow.behavior is PermissionBehavior.ASK
    assert classifier_cannot_allow.reason_code == "mode_default"

    audit_context = context(PermissionMode.APPROVE_FOR_ME)
    audit_context.runtime_state["shell_deterministic_behavior"] = "ask"
    middleware = PermissionMiddleware(no_rules)
    assert (
        asyncio.run(
            middleware.authorize(
                tool,
                arguments,
                ResolvedToolPolicy(external_side_effects=True),
                audit_context,
            )
        )
        is None
    )
    assert audit_context.runtime_state["permission_decision"]["classifier"] == {
        "honored": True,
        "confidence": 0.99,
        "result": "allow",
    }


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
        "read_file",
        "Read.",
        {"type": "object"},
        execute,
        policy=ResolvedToolPolicy.safe_read(),
    )
    engine = PermissionEngine((("project", project),), repository)
    context = _context(tmp_path, permission_mode=PermissionMode.ASK_FOR_APPROVAL)

    async def decide():
        return await engine.decide(tool, {}, ResolvedToolPolicy.safe_read(), context)

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
    assert (
        asyncio.run(engine.decide(allowed, {}, ResolvedToolPolicy(), context)).behavior
        is PermissionBehavior.ALLOW
    )
    assert (
        asyncio.run(engine.decide(other, {}, ResolvedToolPolicy(), context)).behavior
        is PermissionBehavior.ASK
    )


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
    assert assess_shell("printf 'a|b'").behavior == "ask"
    assert assess_shell("git status && rg TODO | head -10").behavior == "ask"
    assert assess_shell("git status && git diff | head -10").behavior == "allow"
    assert assess_shell("git status > status.txt").behavior == "ask"
    assert assess_shell("cat <<'EOF'\nhello\nEOF").behavior == "ask"
    assert assess_shell("sudo rm -rf /").behavior == "deny"
    assert assess_shell("rm -rf $TARGET").behavior == "deny"
    assert assess_shell("echo $(whoami)").behavior == "ask"
    for command in (
        "python -c \"from pathlib import Path; Path('important.py').unlink()\"",
        "git clean -fdx",
        "find . -delete",
        "sed -i 's/a/b/' README.md",
        "ruff format .",
        "git diff --no-index .env README.md",
        "/tmp/git status",
        "git status --output-indicator-new=X",
    ):
        assert assess_shell(command).behavior == "ask"

    class Model:
        def __init__(self):
            self.requests = []

        async def complete(self, **values):
            self.requests.append(values)
            return ModelResponse(
                ModelMessage(
                    '{"behavior":"allow","confidence":0.94,"reason":"looks safe"}'
                ),
                ModelUsage(9, 4),
            )

    model = Model()
    classifier = ModelShellClassifier(model, model_name="fast", threshold=0.95)
    result = asyncio.run(
        classifier.classify(
            command="custom-build", cwd=".", sandbox="default", parsed=("custom-build",)
        )
    )
    assert result.behavior == "ask"
    assert result.audit["honored"] is False
    assert result.audit["input_tokens"] == 9
    assert (
        model.requests[0]["response_format"]["json_schema"]["name"]
        == "shell_classification"
    )
    assert "Return JSON" not in model.requests[0]["messages"][1]["content"]


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
    assert document["config_version"] == CONFIG_VERSION
    assert document["providers"]["main"]["kind"] == "openai_responses"
    assert document["tools"]["schema_budget_tokens"] == 8000
    assert document["tools"]["selection_mode"] == "shadow"
    assert document["tools"]["max_argument_repair_attempts"] == 1
    assert document["providers"]["main"]["strict_tool_calls"] is False
    assert document["providers"]["main"]["json_schema_outputs"] is False
    assert document["shell"]["classifier_threshold"] == 0.95
    backups = list(tmp_path.glob("config.toml.*.bak"))
    assert len(backups) == 1
    assert "config_version = 3" in backups[0].read_text(encoding="utf-8")


def test_previous_config_migrates_provider_to_responses_and_preserves_capabilities(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """config_version = 12
[providers.main]
kind = "openai_compatible"
base_url = "https://example.invalid"
credential = "env:CAPSLOCK_TEST_KEY"
strict_tool_calls = true
[models.main]
provider = "main"
model = "test"
[routing]
reasoning = ["main"]
""",
        encoding="utf-8",
    )

    document = load_config_document(path)

    assert document["config_version"] == CONFIG_VERSION
    assert document["providers"]["main"]["kind"] == "openai_responses"
    assert document["providers"]["main"]["strict_tool_calls"] is True
    assert document["providers"]["main"]["json_schema_outputs"] is False
    assert len(list(tmp_path.glob("config.toml.v12-*.bak"))) == 1


def test_current_config_rejects_chat_completions_provider_kind(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""config_version = {CONFIG_VERSION}
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

    with pytest.raises(
        ValueError, match="unsupported provider kind: openai_compatible"
    ):
        load_config_document(path)
