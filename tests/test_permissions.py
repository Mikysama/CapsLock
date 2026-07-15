from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.document import Document

from capslock import __version__
from capslock.cli.actions import permissions
from capslock.cli.commands import command_completions as _command_completions, command_menu_completions, matching_command_nodes as _matching_command_nodes, resolve_command
from capslock.cli.context import CliContext
from capslock.cli.dispatch import dispatch_slash_command
from capslock.cli.prompt import SlashCommandCompleter, SlashCommandLexer, anchor_completion_menus, permission_rprompt as _permission_rprompt, prompt_footer as _prompt_footer, prompt_tokens as _prompt_tokens, refresh_slash_completion as _refresh_slash_completion
from capslock.cli.render import CAPSLOCK_ART, permission_badge as _permission_badge, render_command_tree, startup_banner as _startup_banner
from capslock.permissions import PermissionMode, assess, requires_approval
from capslock.policy import WorkspacePolicy
from capslock.session import SessionStore
from capslock.theme import make_console
from capslock.tools import RunContext, workspace_tools


def test_permission_modes_and_risk_fallbacks() -> None:
    high = assess("propose_file_edit")
    low = assess("read_file")
    assert requires_approval(PermissionMode.APPROVE_FOR_ME, high)
    assert not requires_approval(PermissionMode.APPROVE_FOR_ME, low)
    assert requires_approval(PermissionMode.ASK_FOR_APPROVAL, low)
    assert not requires_approval(PermissionMode.FULL_ACCESS, high)
    assert "undo" in high.rollback


def test_permission_mode_short_aliases_parse() -> None:
    assert PermissionMode.parse("full") is PermissionMode.FULL_ACCESS
    assert PermissionMode.parse("approve") is PermissionMode.APPROVE_FOR_ME
    assert PermissionMode.parse("ask") is PermissionMode.ASK_FOR_APPROVAL


def test_full_access_auto_applies_edit_with_audit_events(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path / ".capslock" / "state.sqlite3")
    events: list[tuple[str, dict[str, object]]] = []
    context = RunContext(session_id="session", run_id="run", policy=WorkspacePolicy(tmp_path), event=lambda kind, **data: events.append((kind, data)), store=store, permission_mode=PermissionMode.FULL_ACCESS)
    result, _ = workspace_tools().invoke("propose_file_edit", context, {"path": "note.txt", "old_text": "before", "new_text": "after"})
    assert result.ok and result.data["status"] == "completed"
    assert result.data["result_kind"] == "applied"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after"
    assert {kind for kind, _ in events} >= {"risk_assessed", "auto_approved", "change_applied"}
    assert store.last_applied_change("session") is not None


def test_workspace_permission_mode_persists(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / ".capslock" / "state.sqlite3")
    store.set_workspace_setting("permission_mode", PermissionMode.ASK_FOR_APPROVAL.value)
    assert store.workspace_setting("permission_mode") == "ask_for_approval"


def test_permissions_menu_selects_and_persists_mode(tmp_path: Path, monkeypatch) -> None:
    store = SessionStore(tmp_path / ".capslock" / "state.sqlite3")
    agent = SimpleNamespace(permission_mode=PermissionMode.APPROVE_FOR_ME, store=store)
    terminal = make_console(width=100, color_system=None, force_terminal=False, record=True)
    monkeypatch.setattr(terminal, "input", lambda prompt: "1")

    permissions(CliContext(terminal, agent), "/permissions")

    assert agent.permission_mode is PermissionMode.FULL_ACCESS
    assert store.workspace_setting("permission_mode") == "full_access"


def test_permission_badge_is_visible_for_every_mode() -> None:
    assert "完全访问" in _permission_badge(PermissionMode.FULL_ACCESS)
    assert "高风险确认" in _permission_badge(PermissionMode.APPROVE_FOR_ME)
    assert "每次确认" in _permission_badge(PermissionMode.ASK_FOR_APPROVAL)


def test_command_tree_filters_to_current_slash_prefix() -> None:
    permissions = _matching_command_nodes("/permissions a")
    assert [node.command for node in permissions] == ["/permissions"]
    assert [node.command for node in permissions[0].children] == ["/permissions approve", "/permissions ask"]
    assert _command_completions("/mcp t") == ["/mcp tools"]
    assert "/permissions" not in _command_completions("/mcp")
    assert resolve_command("/permissions full").handler == "permissions"
    assert resolve_command("/approve abc123").handler == "approve"
    assert resolve_command("/session").handler == "status"
    assert resolve_command("/quit").handler == "exit"


def test_command_tree_renders_claude_style_flat_two_column_list(monkeypatch) -> None:
    terminal = make_console(width=100, color_system=None, force_terminal=False, record=True)

    render_command_tree(terminal, "/permissions")
    output = terminal.export_text()

    assert "/permissions" in output
    assert "/permissions full" in output
    assert "/permissions approve" in output
    assert "/permissions ask" in output
    assert "├─" not in output


def test_live_command_completer_filters_and_shows_full_command() -> None:
    matches = list(SlashCommandCompleter().get_completions(Document("/mcp t"), object()))
    assert [item.text for item in matches] == ["/mcp tools"]
    assert "".join(fragment[1] for fragment in matches[0].display) == "/mcp tools"


def test_complete_parent_command_expands_its_command_subtree() -> None:
    assert command_menu_completions("/permissions") == [
        "/permissions full",
        "/permissions approve",
        "/permissions ask",
    ]
    matches = list(SlashCommandCompleter().get_completions(Document("/mcp"), object()))
    assert [item.text for item in matches] == ["/mcp list", "/mcp status", "/mcp tools"]


def test_complete_leaf_command_remains_visible_in_command_menu() -> None:
    matches = list(SlashCommandCompleter().get_completions(Document("/exit"), object()))
    assert [item.text for item in matches] == ["/exit "]
    assert "".join(fragment[1] for fragment in matches[0].display) == "/exit"
    assert matches[0].start_position == -len("/exit")


def test_quit_alias_completes_and_dispatches_like_exit() -> None:
    matches = list(SlashCommandCompleter().get_completions(Document("/quit"), object()))
    assert [item.text for item in matches] == ["/quit "]
    assert "".join(fragment[1] for fragment in matches[0].display) == "/quit"
    terminal = make_console(width=100, color_system=None, force_terminal=False, record=True)
    assert dispatch_slash_command(CliContext(terminal, SimpleNamespace()), "/quit") == "exit"


def test_backspace_refreshes_or_closes_slash_completion() -> None:
    calls: list[str] = []

    class BufferStub:
        def __init__(self, text: str) -> None:
            self.document = Document(text)

        def start_completion(self, *, select_first: bool) -> None:
            assert not select_first
            calls.append("start")

        def cancel_completion(self) -> None:
            calls.append("cancel")

    _refresh_slash_completion(BufferStub("/permi"))
    _refresh_slash_completion(BufferStub(""))

    assert calls == ["start", "cancel"]


def test_completion_menu_is_pinned_to_left_edge() -> None:
    from prompt_toolkit.layout.menus import CompletionsMenu

    class FloatStub:
        def __init__(self) -> None:
            self.content = CompletionsMenu()
            self.xcursor = True
            self.left = None

    class ContainerStub:
        def __init__(self, children=(), floats=()) -> None:
            self._children = children
            self.floats = floats

        def get_children(self):
            return self._children

    menu = FloatStub()
    root = ContainerStub(children=(ContainerStub(floats=(menu,)),))

    anchor_completion_menus(root)

    assert menu.xcursor is False
    assert menu.left == 0


def test_slash_command_lexer_applies_bold_command_style() -> None:
    styled = SlashCommandLexer().lex_document(Document("/permissions"))(0)
    plain = SlashCommandLexer().lex_document(Document("hello"))(0)
    assert styled == [("class:slash-command", "/permissions")]
    assert plain == [("class:user-input", "hello")]


def test_prompt_has_input_borders_footer_and_permission_rprompt() -> None:
    prompt = "".join(fragment[1] for fragment in _prompt_tokens(PermissionMode.APPROVE_FOR_ME, width=40))
    footer = "".join(fragment[1] for fragment in _prompt_footer(width=40))
    rprompt = "".join(fragment[1] for fragment in _permission_rprompt(PermissionMode.APPROVE_FOR_ME))
    assert prompt == "─" * 39 + "\n❯ "
    assert footer == "─" * 39 + "\n? /help  ·  ↑/↓ 选择  ·  Tab 补全"
    assert rprompt.strip() == "approve_for_me"


def test_startup_banner_contains_capslock_symbol_wordmark_and_session_identity() -> None:
    workspace = Path("/tmp/project")
    agent = SimpleNamespace(
        model="deepseek-v4-flash",
        permission_mode=PermissionMode.APPROVE_FOR_ME,
        workspace=workspace,
    )
    terminal = make_console(width=120, color_system=None, force_terminal=False, record=True)

    terminal.print(_startup_banner(agent, width=120))
    output = terminal.export_text()

    assert output.count("⇪") == 79
    assert "⇪⇪⇪  ⇪⇪⇪  ⇪⇪⇪  ⇪⇪⇪" in output
    assert "deepseek-v4-flash" in output
    assert "project" in output
    assert f"v{__version__}" in output
    assert len(CAPSLOCK_ART) == 5
    assert {len(line) for line in CAPSLOCK_ART} == {38}
    art_lines = [line for line in output.splitlines() if "⇪" in line]
    assert len(art_lines) == 5
    assert len({line.index("⇪") for line in art_lines}) == 1
