from types import SimpleNamespace

from capslock.cli.commands import command_menu_completions
from capslock.cli.context import CliContext
from capslock.cli.memory import memory_command
from capslock.domain import MemoryScope, MemoryType
from capslock.memory import MemoryService
from capslock.storage import MemoryStore
from capslock.theme import make_console


def test_memory_command_tree_exposes_all_management_operations() -> None:
    assert command_menu_completions("/memory") == [
        "/memory list",
        "/memory search",
        "/memory show",
        "/memory add",
        "/memory edit",
        "/memory forget",
        "/memory undo",
        "/memory purge",
        "/memory export",
        "/memory import",
        "/memory status",
        "/memory enable",
        "/memory disable",
        "/memory policy",
        "/memory recall",
        "/memory candidates",
        "/memory candidate",
        "/memory context",
        "/memory cleanup",
        "/memory embeddings",
    ]


def test_memory_cli_add_search_forget_and_policy(tmp_path) -> None:
    store = MemoryStore(tmp_path / "user.sqlite3")
    memory = MemoryService(store, workspace=tmp_path, session_id="session")
    console = make_console(width=120, color_system=None, force_terminal=False, record=True)
    answers = iter(["2", "1", "api_key=cli-secret useful fact", "", ""])
    console.input = lambda *args, **kwargs: next(answers)
    context = CliContext(console, SimpleNamespace(memory=memory))

    memory_command(context, "/memory add")
    item = memory.list()[0]
    assert item.scope is MemoryScope.WORKSPACE
    assert item.type is MemoryType.FACT
    assert item.content == "api_key=<redacted> useful fact"
    memory_command(context, "/memory search useful")
    memory_command(context, f"/memory forget {item.id[:12]}")
    memory_command(context, f"/memory undo {item.id[:12]}")
    memory_command(context, "/memory disable")
    memory_command(context, "/memory status")

    output = console.export_text()
    assert "Saved memory" in output
    assert "secret_field" in output
    assert "cli-secret" not in output
    assert "effective_write_enabled=False" in output
    store.close()
