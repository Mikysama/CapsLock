import json
import shutil
from pathlib import Path

import pytest

from capslock.cli.commands import command_menu_completions
from capslock.cli.skills import parse_skill_input
from capslock.model import ModelMessage, ModelResponse, ModelToolCall
from capslock.runtime import AgentRuntimeError, WorkspaceAgent
from capslock.session import SessionStore
from capslock.skills import SkillRegistry, SkillValidationError, load_skill_package
from capslock.tools import workspace_tools


INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["topic"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


def make_skill(
    root: Path,
    name: str = "demo",
    *,
    tools: tuple[str, ...] = (),
    permissions: tuple[str, ...] = (),
    description: str = "Demo Skill",
    minimum: str = "1.4.0",
) -> Path:
    package = root / name
    (package / "schemas").mkdir(parents=True)
    package.joinpath("skill.toml").write_text(
        "\n".join(
            (
                "schema_version = 1",
                f'name = "{name}"',
                'version = "1.0.0"',
                f'description = "{description}"',
                f'min_capslock_version = "{minimum}"',
                'instructions = "instructions.md"',
                'input_schema = "schemas/input.json"',
                'output_schema = "schemas/output.json"',
                f"required_tools = {json.dumps(tools)}",
                f"required_permissions = {json.dumps(permissions)}",
            )
        ),
        encoding="utf-8",
    )
    package.joinpath("instructions.md").write_text(
        "Summarize the requested topic.", encoding="utf-8"
    )
    package.joinpath("schemas/input.json").write_text(
        json.dumps(INPUT_SCHEMA), encoding="utf-8"
    )
    package.joinpath("schemas/output.json").write_text(
        json.dumps(OUTPUT_SCHEMA), encoding="utf-8"
    )
    return package


class Model:
    def __init__(self, *messages: ModelMessage, before_first=None) -> None:
        self.messages = list(messages)
        self.calls = []
        self.before_first = before_first

    def complete(self, **kwargs) -> ModelResponse:
        self.calls.append(kwargs)
        if len(self.calls) == 1 and self.before_first:
            self.before_first()
        return ModelResponse(self.messages.pop(0))


def test_manifest_validation_and_workspace_override(tmp_path: Path) -> None:
    user = tmp_path / "user"
    workspace = tmp_path / "workspace"
    make_skill(user, description="User copy")
    make_skill(workspace / "capslock.skills", description="Workspace copy")
    disabled = set()
    registry = SkillRegistry(
        workspace,
        available_tools=workspace_tools().names,
        disabled=lambda name: name in disabled,
        user_root=user,
        current_version="1.5.0",
    )

    assert registry.get("demo").scope == "workspace"
    assert registry.get("demo").manifest.description == "Workspace copy"
    disabled.add("demo")
    with pytest.raises(SkillValidationError, match="disabled"):
        registry.get("demo")
    assert registry.get("demo", require_enabled=False).scope == "workspace"


def test_invalid_workspace_override_blocks_user_fallback(tmp_path: Path) -> None:
    user = tmp_path / "user"
    workspace = tmp_path / "workspace"
    make_skill(user)
    package = make_skill(workspace / "capslock.skills")
    package.joinpath("skill.toml").write_text(
        package.joinpath("skill.toml").read_text(encoding="utf-8") + "\nunknown = true\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(
        workspace,
        available_tools=workspace_tools().names,
        user_root=user,
        current_version="1.5.0",
    )

    with pytest.raises(SkillValidationError, match="Unknown Skill manifest fields"):
        registry.get("demo")


def test_new_and_legacy_skill_roots_merge_or_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    old = make_skill(workspace / "capslock.skills")
    new = workspace / ".capslock/skills/demo"
    shutil.copytree(old, new)
    registry = SkillRegistry(
        workspace,
        available_tools=workspace_tools().names,
        current_version="1.5.0",
    )

    assert registry.get("demo").root == new

    new.joinpath("instructions.md").write_text("different", encoding="utf-8")
    with pytest.raises(SkillValidationError, match="conflicting new and legacy workspace Skill"):
        registry.get("demo")


def test_manifest_rejects_unsafe_paths_versions_and_missing_permissions(tmp_path: Path) -> None:
    package = make_skill(tmp_path, minimum="9.0.0")
    with pytest.raises(SkillValidationError, match="requires CapsLock"):
        load_skill_package(
            package, scope="workspace", current_version="1.5.0", available_tools=workspace_tools().names
        )

    shutil.rmtree(package)
    package = make_skill(
        tmp_path,
        tools=("command:pytest",),
        permissions=(),
    )
    with pytest.raises(SkillValidationError, match="required_permissions is missing"):
        load_skill_package(
            package, scope="workspace", current_version="1.5.0", available_tools=workspace_tools().names
        )

    package.joinpath("skill.toml").write_text(
        package.joinpath("skill.toml").read_text(encoding="utf-8").replace(
            'instructions = "instructions.md"', 'instructions = "../outside.md"'
        ).replace('required_permissions = []', 'required_permissions = ["command.execute"]'),
        encoding="utf-8",
    )
    with pytest.raises(SkillValidationError, match="package-relative"):
        load_skill_package(
            package, scope="workspace", current_version="1.5.0", available_tools=workspace_tools().names
        )

    shutil.rmtree(package)
    package = make_skill(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    package.joinpath("instructions.md").unlink()
    package.joinpath("instructions.md").symlink_to(outside)
    with pytest.raises(SkillValidationError, match="symlink"):
        load_skill_package(
            package, scope="workspace", current_version="1.5.0", available_tools=workspace_tools().names
        )

    package.joinpath("instructions.md").unlink()
    package.joinpath("instructions.md").write_text("instructions", encoding="utf-8")
    unsafe_schema = {**INPUT_SCHEMA, "$dynamicRef": "https://example.com/schema"}
    package.joinpath("schemas/input.json").write_text(
        json.dumps(unsafe_schema), encoding="utf-8"
    )
    with pytest.raises(SkillValidationError, match="internal"):
        load_skill_package(
            package, scope="workspace", current_version="1.5.0", available_tools=workspace_tools().names
        )


def test_skill_input_parsing_uses_schema_and_interactive_required_fields(tmp_path: Path) -> None:
    package = load_skill_package(
        make_skill(tmp_path),
        scope="workspace",
        current_version="1.5.0",
        available_tools=workspace_tools().names,
    )
    prompted = []

    result = parse_skill_input(
        package,
        ["limit=3"],
        lambda prompt: prompted.append(prompt) or "security",
    )

    assert result == {"limit": 3, "topic": "security"}
    assert prompted == ["topic> "]
    with pytest.raises(ValueError, match="duplicate"):
        parse_skill_input(package, ["topic=one", "topic=two"], lambda prompt: "")
    with pytest.raises(SkillValidationError) as invalid:
        package.validate_input({"topic": 123456789})
    assert "123456789" not in str(invalid.value)


def test_skill_run_validates_output_and_preserves_audit(tmp_path: Path) -> None:
    make_skill(tmp_path / "capslock.skills")
    store = SessionStore(tmp_path / ".capslock" / "state.sqlite3")
    agent = WorkspaceAgent(
        Model(ModelMessage('{"summary":"done"}')),
        workspace=tmp_path,
        model="test",
        store=store,
    )

    answer = agent.run_skill("demo", {"topic": "release"})
    run = store.get_skill_run(answer.run_id)

    assert answer.output == {"summary": "done"}
    assert run is not None and run.status == "completed"
    assert run.name == "demo" and run.input == {"topic": "release"}
    shutil.rmtree(tmp_path / "capslock.skills" / "demo")
    assert store.get_skill_run(answer.run_id).manifest_digest == run.manifest_digest


def test_skill_run_rejects_invalid_structured_output(tmp_path: Path) -> None:
    make_skill(tmp_path / "capslock.skills")
    store = SessionStore(tmp_path / ".capslock" / "state.sqlite3")
    agent = WorkspaceAgent(
        Model(ModelMessage("not json")), workspace=tmp_path, model="test", store=store
    )

    with pytest.raises(AgentRuntimeError, match="raw JSON"):
        agent.run_skill("demo", {"topic": "release"})

    run = store.list_skill_runs(agent.session_id)[0]
    assert run.status == "failed" and "raw JSON" in run.error


def test_skill_fixtures_check_valid_and_expected_failure_cases(tmp_path: Path) -> None:
    package = make_skill(tmp_path)
    fixtures = package / "fixtures"
    fixtures.mkdir()
    fixtures.joinpath("valid.json").write_text(
        json.dumps(
            {
                "input": {"topic": "release"},
                "output": {"summary": "done"},
            }
        ),
        encoding="utf-8",
    )
    fixtures.joinpath("failure.json").write_text(
        json.dumps({"input": {}, "input_valid": False}), encoding="utf-8"
    )
    first = load_skill_package(
        package,
        scope="workspace",
        current_version="1.5.0",
        available_tools=workspace_tools().names,
    )

    valid = json.loads(fixtures.joinpath("valid.json").read_text(encoding="utf-8"))
    valid["output"]["summary"] = "changed"
    fixtures.joinpath("valid.json").write_text(json.dumps(valid), encoding="utf-8")
    second = load_skill_package(
        package,
        scope="workspace",
        current_version="1.5.0",
        available_tools=workspace_tools().names,
    )
    assert first.digest != second.digest

    fixtures.joinpath("failure.json").write_text(
        json.dumps({"input": {}, "input_valid": True}), encoding="utf-8"
    )
    with pytest.raises(SkillValidationError, match="expected input to be valid"):
        load_skill_package(
            package,
            scope="workspace",
            current_version="1.5.0",
            available_tools=workspace_tools().names,
        )


def test_qualified_command_is_rechecked_and_audited(tmp_path: Path) -> None:
    make_skill(
        tmp_path / "capslock.skills",
        tools=("command:pytest",),
        permissions=("command.execute",),
    )
    call = ModelToolCall(
        "call",
        "propose_command",
        json.dumps({"template": "ruff_check"}),
    )
    store = SessionStore(tmp_path / ".capslock" / "state.sqlite3")
    agent = WorkspaceAgent(
        Model(ModelMessage(None, (call,)), ModelMessage('{"summary":"blocked"}')),
        workspace=tmp_path,
        model="test",
        store=store,
    )

    answer = agent.run_skill("demo", {"topic": "release"})
    row = store._connection.execute(
        "SELECT ok,result_summary FROM tool_calls WHERE run_id=?", (answer.run_id,)
    ).fetchone()

    assert row["ok"] == 0
    assert "did not declare command template" in row["result_summary"]
    assert store._connection.execute("SELECT count(*) FROM actions").fetchone()[0] == 0


def test_skill_change_during_run_aborts_execution(tmp_path: Path) -> None:
    package = make_skill(
        tmp_path / "capslock.skills",
        tools=("read_file",),
        permissions=("workspace.read",),
    )
    tmp_path.joinpath("note.txt").write_text("content", encoding="utf-8")
    call = ModelToolCall("call", "read_file", json.dumps({"path": "note.txt"}))
    store = SessionStore(tmp_path / ".capslock" / "state.sqlite3")
    model = Model(ModelMessage(None, (call,)), before_first=lambda: shutil.rmtree(package))
    agent = WorkspaceAgent(model, workspace=tmp_path, model="test", store=store)

    with pytest.raises(AgentRuntimeError, match="not registered"):
        agent.run_skill("demo", {"topic": "release"})

    assert store.list_skill_runs(agent.session_id)[0].status == "failed"


def test_skill_command_tree_exposes_manual_operations() -> None:
    assert command_menu_completions("/skills") == [
        "/skills list",
        "/skills show",
        "/skills validate",
        "/skills run",
        "/skills enable",
        "/skills disable",
    ]
