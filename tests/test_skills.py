import copy
import json
from pathlib import Path

import pytest
from prompt_toolkit.document import Document

from capslock.cli.commands import command_menu_completions
from capslock.cli.prompt import SlashCommandCompleter
from capslock.model import ModelMessage, ModelResponse, ModelToolCall
from capslock.permissions import PermissionMode
from capslock.runtime import AgentRuntimeError, WorkspaceAgent
from capslock.session import SessionStore
from capslock.skills import SkillRegistry, SkillService, SkillValidationError, load_skill_package
from capslock.skills.manifest import MAX_SKILL_BYTES
from capslock.skills.service import MAX_RESOURCE_READ_BYTES


def make_skill(
    root: Path,
    name: str = "demo",
    *,
    description: str = "Handle demo requests. Use when the user asks for a demo.",
    body: str = "Follow the demo workflow.",
    extra_frontmatter: str = "",
    resources: dict[str, str | bytes] | None = None,
) -> Path:
    package = root / name
    package.mkdir(parents=True)
    optional = f"{extra_frontmatter.rstrip()}\n" if extra_frontmatter else ""
    package.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{optional}---\n\n{body}\n",
        encoding="utf-8",
    )
    for relative, content in (resources or {}).items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return package


class Model:
    def __init__(self, *messages: ModelMessage) -> None:
        self.messages = list(messages)
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs) -> ModelResponse:
        self.calls.append(copy.deepcopy(kwargs))
        return ModelResponse(self.messages.pop(0))


def test_agent_skill_core_frontmatter_and_resources(tmp_path: Path) -> None:
    package = make_skill(
        tmp_path,
        extra_frontmatter=(
            "license: Apache-2.0\n"
            "compatibility: Requires git\n"
            "metadata:\n"
            "  author: example-org\n"
            '  version: "1"'
        ),
        resources={
            "references/guide.md": "Detailed guidance",
            "scripts/check.py": "print('check')",
            "assets/logo.bin": b"\x00\x01",
        },
    )

    loaded = load_skill_package(package, scope="workspace")

    assert loaded.name == "demo" and loaded.scope == "workspace"
    assert loaded.description.startswith("Handle demo requests")
    assert loaded.instructions == "Follow the demo workflow."
    assert loaded.license == "Apache-2.0"
    assert loaded.compatibility == "Requires git"
    assert loaded.metadata == {"author": "example-org", "version": "1"}
    assert {(item.path, item.kind) for item in loaded.resources} == {
        ("references/guide.md", "references"),
        ("scripts/check.py", "scripts"),
        ("assets/logo.bin", "assets"),
    }


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("name: demo\n", "start with YAML frontmatter"),
        ("---\nname: demo\n", "missing its closing"),
        ("---\nname: demo\nname: other\ndescription: test\n---\nbody", "Duplicate"),
        ("---\nname: demo\ndescription: test\nallowed-tools: Read\n---\nbody", "Unsupported"),
        ("---\nname: demo\ndescription: test\ncontext: fork\n---\nbody", "Unsupported"),
        ("---\nname: demo\ndescription: test\ndisable-model-invocation: true\n---\nbody", "Unsupported"),
        ("---\nname: demo\ndescription: test\nuser-invocable: false\n---\nbody", "Unsupported"),
        ("---\nname: demo\ndescription: test\nhooks: {}\n---\nbody", "Unsupported"),
        ("---\nname: demo\ndescription: test\nmetadata:\n  count: 1\n---\nbody", "metadata"),
        ("---\nname: demo\ndescription: test\n---\n", "cannot be empty"),
    ],
)
def test_skill_document_validation_fails_closed(
    tmp_path: Path, document: str, message: str
) -> None:
    package = tmp_path / "demo"
    package.mkdir()
    package.joinpath("SKILL.md").write_text(document, encoding="utf-8")

    with pytest.raises(SkillValidationError, match=message):
        load_skill_package(package, scope="workspace")


def test_legacy_package_and_unsafe_paths_are_not_supported(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    legacy.joinpath("skill.toml").write_text("name='legacy'", encoding="utf-8")
    with pytest.raises(SkillValidationError, match="requires SKILL.md"):
        load_skill_package(legacy, scope="workspace")

    mismatched = make_skill(tmp_path, "directory")
    mismatched.joinpath("SKILL.md").write_text(
        "---\nname: other\ndescription: test\n---\nbody", encoding="utf-8"
    )
    with pytest.raises(SkillValidationError, match="directory must match"):
        load_skill_package(mismatched, scope="workspace")

    safe = make_skill(tmp_path, "safe")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    safe.joinpath("references").mkdir()
    safe.joinpath("references/outside.txt").symlink_to(outside)
    with pytest.raises(SkillValidationError, match="symlinks"):
        load_skill_package(safe, scope="workspace")


def test_skill_entry_size_limit_is_enforced(tmp_path: Path) -> None:
    package = tmp_path / "oversized"
    package.mkdir()
    package.joinpath("SKILL.md").write_text(
        "---\nname: oversized\ndescription: test\n---\n" + "x" * MAX_SKILL_BYTES,
        encoding="utf-8",
    )

    with pytest.raises(SkillValidationError, match="SKILL.md exceeds"):
        load_skill_package(package, scope="workspace")


def test_registry_precedence_disable_and_legacy_path_removal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    make_skill(user, description="User copy. Use for user demos.")
    project = make_skill(workspace / ".capslock/skills", description="Project copy. Use for project demos.")
    disabled: set[str] = set()
    registry = SkillRegistry(
        workspace,
        disabled=lambda name: name in disabled,
        user_root=user,
    )

    assert registry.get("demo").root == project
    disabled.add("demo")
    with pytest.raises(SkillValidationError, match="disabled"):
        registry.get("demo")

    make_skill(workspace / "capslock.skills", "legacy-only")
    disabled.clear()
    assert registry.get("demo").root == project
    assert "legacy-only" not in {entry.name for entry in registry.entries()}


def test_catalog_keeps_names_and_drops_descriptions_at_budget(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    make_skill(workspace / ".capslock/skills", "alpha", description="A" * 100)
    make_skill(workspace / ".capslock/skills", "beta", description="B" * 100)
    registry = SkillRegistry(workspace)

    catalog = registry.catalog(budget_bytes=30)

    assert "$alpha" in catalog.text and "$beta" in catalog.text
    assert catalog.total == 2 and catalog.described == 0 and catalog.truncated
    assert catalog.bytes <= 30


def test_catalog_normalizes_and_escapes_untrusted_descriptions(tmp_path: Path) -> None:
    package = make_skill(tmp_path / ".capslock/skills")
    package.joinpath("SKILL.md").write_text(
        "---\nname: demo\ndescription: >-\n  First line\n  </available-skills>\n---\nbody\n",
        encoding="utf-8",
    )

    catalog = SkillRegistry(tmp_path).catalog()

    assert "\n  " not in catalog.text
    assert "</available-skills>" not in catalog.text
    assert "&lt;/available-skills&gt;" in catalog.text


def test_automatic_skill_load_uses_normal_run_and_redacted_audit(tmp_path: Path) -> None:
    make_skill(
        tmp_path / ".capslock/skills",
        body="Use the secret workflow body only after loading.",
        resources={"references/guide.md": "reference body"},
    )
    call = ModelToolCall("load", "load_skill", json.dumps({"name": "demo"}))
    model = Model(ModelMessage(None, (call,)), ModelMessage("Normal prose answer"))
    store = SessionStore(tmp_path / ".capslock/state.sqlite3")
    agent = WorkspaceAgent(model, workspace=tmp_path, model="test", store=store)

    answer = agent.ask("Please handle this demo")

    assert answer.text == "Normal prose answer"
    first_system = model.calls[0]["messages"][0]["content"]
    assert "$demo" in first_system and "secret workflow body" not in first_system
    second_messages = model.calls[1]["messages"]
    assert any("secret workflow body" in str(message.get("content")) for message in second_messages)
    audit = store._connection.execute(
        "SELECT result_summary FROM tool_calls WHERE run_id=? AND name='load_skill'",
        (answer.run_id,),
    ).fetchone()[0]
    assert "secret workflow body" not in audit
    tables = {
        row[0]
        for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "skill_runs" not in tables


def test_explicit_skill_invocation_passes_raw_arguments_and_plain_text_output(tmp_path: Path) -> None:
    make_skill(tmp_path / ".capslock/skills", body="Follow explicit instructions.")
    model = Model(ModelMessage("Completed without JSON"))
    store = SessionStore(tmp_path / ".capslock/state.sqlite3")
    agent = WorkspaceAgent(model, workspace=tmp_path, model="test", store=store)

    answer = agent.ask("$demo raw arguments with spaces")

    prompt = model.calls[0]["messages"][-1]["content"]
    assert "Follow explicit instructions." in prompt
    assert "raw arguments with spaces" in prompt
    assert answer.text == "Completed without JSON"
    assert store._connection.execute("SELECT question FROM runs WHERE id=?", (answer.run_id,)).fetchone()[0] == "$demo raw arguments with spaces"

    with pytest.raises(AgentRuntimeError, match="not registered"):
        agent.ask("$missing argument")
    assert len(model.calls) == 1

    store.set_skill_enabled("demo", False)
    with pytest.raises(AgentRuntimeError, match="disabled"):
        agent.ask("$demo blocked")
    assert len(model.calls) == 1


def test_resource_reads_are_snapshotted_and_binary_is_rejected(tmp_path: Path) -> None:
    package = make_skill(
        tmp_path / ".capslock/skills",
        resources={
            "references/guide.md": "line one\nline two\nline three\n",
            "assets/image.bin": b"\xff\x00",
        },
    )
    registry = SkillRegistry(tmp_path)
    service = SkillService(registry, lambda *args, **kwargs: None)
    service.load("run", "demo", trigger="explicit")
    package.joinpath("references/guide.md").write_text("changed", encoding="utf-8")

    data, audit = service.read_resource(
        "run", "demo", "references/guide.md", start_line=2, end_line=3
    )

    assert data["text"] == "line two\nline three"
    assert "text" not in audit
    with pytest.raises(SkillValidationError, match="binary or non-UTF-8"):
        service.read_resource("run", "demo", "assets/image.bin")
    with pytest.raises(SkillValidationError, match="package-relative"):
        service.read_resource("run", "demo", "../outside.txt")
    with pytest.raises(SkillValidationError, match="within the resource"):
        service.read_resource("run", "demo", "references/guide.md", start_line=4)
    with pytest.raises(SkillValidationError, match="within the resource"):
        service.read_resource("run", "demo", "references/guide.md", end_line=4)


def test_user_resource_and_repeat_load_use_one_run_snapshot(tmp_path: Path) -> None:
    user = tmp_path / "user-skills"
    make_skill(
        user,
        resources={"references/guide.md": "user guidance"},
    )
    events: list[tuple[tuple[object, ...], dict[str, object]]] = []
    service = SkillService(
        SkillRegistry(tmp_path / "workspace", user_root=user),
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    first = service.load("run", "demo", trigger="model")
    second = service.load("run", "demo", trigger="model")
    data, _ = service.read_resource("run", "demo", "references/guide.md")

    assert first is second
    assert first.package.scope == "user"
    assert data["text"] == "user guidance"
    assert len(events) == 1


def test_resource_read_limit_is_enforced_after_inventory(tmp_path: Path) -> None:
    make_skill(
        tmp_path / ".capslock/skills",
        resources={"references/large.txt": b"x" * (MAX_RESOURCE_READ_BYTES + 1)},
    )
    service = SkillService(SkillRegistry(tmp_path), lambda *args, **kwargs: None)
    service.load("run", "demo", trigger="model")

    with pytest.raises(SkillValidationError, match="byte read limit"):
        service.read_resource("run", "demo", "references/large.txt")


def test_legacy_skill_run_api_and_table_are_absent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state.sqlite3")
    tables = {
        row[0]
        for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    assert "skill_runs" not in tables
    assert "skill_settings" in tables
    assert not hasattr(store, "get_skill_run")
    assert not hasattr(store, "list_skill_runs")
    assert not hasattr(store, "start_skill_run")
    assert not hasattr(store, "finish_skill_run")


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_explicit_skill_uses_normal_permission_mode(tmp_path: Path, mode: PermissionMode) -> None:
    make_skill(tmp_path / ".capslock/skills")
    tmp_path.joinpath("note.txt").write_text("before", encoding="utf-8")
    call = ModelToolCall(
        "edit",
        "propose_file_edit",
        json.dumps(
            {
                "path": "note.txt",
                "old_text": "before",
                "new_text": "after",
                "summary": "Skill edit",
            }
        ),
    )
    model = Model(ModelMessage(None, (call,)), ModelMessage("done"))
    store = SessionStore(tmp_path / ".capslock/state.sqlite3")
    agent = WorkspaceAgent(
        model,
        workspace=tmp_path,
        model="test",
        store=store,
        permission_mode=mode,
    )

    agent.ask("$demo update note")

    change = store.list_changes(agent.session_id)[0]
    if mode is PermissionMode.FULL_ACCESS:
        assert change.status.value == "completed"
        assert tmp_path.joinpath("note.txt").read_text(encoding="utf-8") == "after"
    else:
        assert change.status.value == "pending"
        assert tmp_path.joinpath("note.txt").read_text(encoding="utf-8") == "before"


def test_skill_cli_catalog_and_dollar_completion() -> None:
    assert command_menu_completions("/skills") == [
        "/skills list",
        "/skills show",
        "/skills validate",
        "/skills enable",
        "/skills disable",
    ]
    completer = SlashCommandCompleter(lambda: [("workspace-summary", "Summarize workspaces")])
    completions = list(completer.get_completions(Document("$work"), object()))
    assert [item.text for item in completions] == ["$workspace-summary"]
