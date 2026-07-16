"""Reusable Rich render components for the terminal interface."""

from __future__ import annotations

import shutil

from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import __version__
from ..permissions import PermissionMode
from ..runtime import WorkspaceAnswer
from .commands import command_completions, command_descriptions


_CAPSLOCK_FONT = {
    "C": ("⇪⇪⇪", "⇪  ", "⇪  ", "⇪  ", "⇪⇪⇪"), "A": ("⇪⇪⇪", "⇪ ⇪", "⇪⇪⇪", "⇪ ⇪", "⇪ ⇪"),
    "P": ("⇪⇪⇪", "⇪ ⇪", "⇪⇪⇪", "⇪  ", "⇪  "), "S": ("⇪⇪⇪", "⇪  ", "⇪⇪⇪", "  ⇪", "⇪⇪⇪"),
    "L": ("⇪  ", "⇪  ", "⇪  ", "⇪  ", "⇪⇪⇪"), "O": ("⇪⇪⇪", "⇪ ⇪", "⇪ ⇪", "⇪ ⇪", "⇪⇪⇪"),
    "K": ("⇪ ⇪", "⇪⇪ ", "⇪  ", "⇪⇪ ", "⇪ ⇪"),
}
CAPSLOCK_ART = tuple("  ".join(_CAPSLOCK_FONT[letter][row] for letter in "CAPSLOCK") for row in range(5))


def capslock_logo() -> Text:
    logo = Text(no_wrap=True)
    for index, line in enumerate(CAPSLOCK_ART):
        logo.append(line, style="primary.bold")
        if index < len(CAPSLOCK_ART) - 1:
            logo.append("\n")
    return logo


def startup_banner(agent: object, width: int | None = None) -> Panel:
    terminal_width = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    identity = Text.assemble(("Welcome back!\n", "text.primary.bold"), (f"{agent.model}", "text.secondary"), ("  ·  ", "text.muted"), (agent.permission_mode.value, "waiting"), (f"\n{agent.workspace}", "path"))
    tips = Text.assemble(("Tips for getting started\n", "primary.bold"), ("Type ", "text.primary"), ("/", "command.bold"), (" to browse commands and ", "text.primary"), ("/status", "command.bold"), (" to inspect this session.\n\n", "text.primary"), ("CapsLock keeps workspace actions visible, reviewable, and reversible.", "text.secondary"))
    if terminal_width < 110:
        body = Group(Align.center(identity), Align.center(capslock_logo()))
    else:
        body = Table.grid(expand=True, padding=(0, 2))
        body.add_column(ratio=2)
        body.add_column(ratio=3)
        body.add_row(Align.center(Group(identity, Text(""), capslock_logo()), vertical="middle"), tips)
    return Panel(body, title=Text(f" CapsLock v{__version__} ", style="agent.bold"), title_align="left", border_style="border.focus", padding=(1, 2))


def permission_badge(mode: PermissionMode) -> str:
    return {PermissionMode.FULL_ACCESS: "[error.bold]权限：完全访问[/]", PermissionMode.APPROVE_FOR_ME: "[waiting.bold]权限：高风险确认[/]", PermissionMode.ASK_FOR_APPROVAL: "[thinking.bold]权限：每次确认[/]"}[mode]


def table(*columns: str) -> Table:
    return Table(*columns, header_style="primary.soft.bold", border_style="border.muted", row_styles=("text.primary",))


def status_text(status: str) -> Text:
    normalized = status.casefold()
    if normalized in {"completed", "applied", "approved", "success", "succeeded"}:
        style = "success"
    elif normalized in {"failed", "error", "rejected", "cancelled"}:
        style = "error"
    elif normalized in {"running", "executing", "in_progress"}:
        style = "running"
    elif normalized in {"pending", "waiting", "proposed"}:
        style = "waiting"
    else:
        style = "text.secondary"
    return Text(status, style=style)


def render_command_tree(console: Console, prefix: str = "/") -> None:
    """Render the matching commands as a fixed two-column list."""
    matches = command_completions(prefix)
    descriptions = command_descriptions()
    if not matches:
        console.print("[warning]没有匹配的指令。[/]")
        return
    output = table("Command", "Description")
    for command in matches:
        output.add_row(command, descriptions[command])
    console.print(output)


def select_command(console: Console) -> str | None:
    descriptions = command_descriptions()
    commands = command_completions("/")
    render_command_tree(console, "/")
    options = tuple((command, f"{command} — {descriptions[command]}") for command in commands)
    choice = select_choice(console, "选择指令", options + (("cancel", "取消"),), escape_key="cancel")
    return None if choice == "cancel" else choice


def select_choice(
    console: Console,
    title: str,
    options: tuple[tuple[str, str], ...],
    *,
    default: int = 0,
    escape_key: str | None = None,
) -> str:
    if not options:
        raise ValueError("selection options must not be empty")
    escape_key = escape_key or options[-1][0]
    console.print(selection_menu(title, options))
    answer = console.input(f"[primary.bold]选择 1-{len(options)}（Enter 默认 {default + 1}）>[/] ").strip().casefold()
    if not answer:
        return options[default][0]
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1][0]
    for key, label in options:
        if answer in {key.casefold(), key[0].casefold(), label.casefold()}:
            return key
    console.print("[warning]未识别的选择，已取消。[/]")
    return escape_key


def selection_menu(title: str, options: tuple[tuple[str, str], ...]) -> Panel:
    body = Text()
    for index, (_, label) in enumerate(options, start=1):
        body.append(f"{index}. ", style="primary.bold")
        body.append(f"{label}\n", style="text.primary")
    return Panel(body, title=title, title_align="left", border_style="border.focus")


def render_answer(console: Console, answer: WorkspaceAnswer, debug: bool) -> None:
    console.print(f"\n[agent.bold]◆ Capslock[/] {answer.text}")
    for item in answer.citations:
        if hasattr(item, "scope"):
            console.print(
                f"  [text.secondary]Memory:[/] {item.type.value} · {item.scope.value} · "
                f"[text.muted]{item.source_kind} ({item.id})[/]"
            )
        elif hasattr(item, "path"):
            console.print(
                f"  [text.secondary]Evidence:[/] [path]{item.path}[/]:L{item.start_line}-L{item.end_line} "
                f"[text.muted]({item.id})[/]"
            )
        else:
            console.print(
                f"  [text.secondary]Source:[/] {item.title} · {item.url} · "
                f"[text.muted]{item.fetched_at} ({item.id})[/]"
            )
    console.print(f"  [text.muted]Run {answer.run_id[:8]} · {answer.duration_ms}ms[/]")
    if debug:
        for event in answer.events:
            console.print(f"  [text.muted]{event.kind}: {event.data}[/]")


def render_changes(console: Console, changes: list[object], *, pending_only: bool = False) -> None:
    if not changes:
        if not pending_only:
            console.print("[text.secondary]No change proposals in this session.[/]")
        return
    output = table("Change", "Status", "Result", "Operation", "Path", "Summary")
    for item in changes:
        output.add_row(
            Text(item.id[:12], style="text.muted"),
            status_text(item.status),
            Text(item.result_kind or "", style="text.secondary"),
            Text(item.operation, style="text.secondary"),
            Text(item.path, style="path"),
            Text(item.summary, style="text.secondary"),
        )
    console.print(output)
    for item in changes:
        if item.status == "pending":
            console.print(
                Panel(
                    Text(item.diff or "(no textual diff)", style="code"),
                    title=f"Review {item.id[:12]} · {item.path}",
                    border_style="warning",
                )
            )


def render_commands(console: Console, commands: list[object]) -> None:
    if not commands:
        console.print("[text.secondary]No command proposals in this session.[/]")
        return
    output = table("Command", "Status", "Result", "CWD", "Exit", "Command")
    for item in commands:
        output.add_row(
            Text(item.id[:12], style="text.muted"),
            status_text(item.status),
            Text(item.result_kind or "", style="text.secondary"),
            Text(item.cwd, style="path"),
            Text(str(item.exit_code) if item.exit_code is not None else "", style="text.muted"),
            Text(" ".join(item.argv), style="command"),
        )
    console.print(output)


def render_tasks(console: Console, tasks: list[object]) -> None:
    if not tasks:
        console.print("[text.secondary]No tasks in this session.[/]")
        return
    output = table("Task", "Status", "Text")
    for item in tasks:
        output.add_row(Text(item.id[:12], style="text.muted"), status_text(item.status), item.text)
    console.print(output)


def render_cost(console: Console, input_tokens: int, output_tokens: int, cost: float) -> None:
    console.print(
        Text.assemble(
            ("input_tokens=", "text.muted"),
            (f"{input_tokens}\n", "text.secondary"),
            ("output_tokens=", "text.muted"),
            (f"{output_tokens}\n", "text.secondary"),
            ("cost_usd=", "text.muted"),
            (f"${cost:.6f}", "text.secondary"),
        )
    )


def render_external_actions(console: Console, actions: list[object]) -> None:
    if not actions:
        console.print("[text.secondary]No external action proposals in this session.[/]")
        return
    output = table("Action", "Status", "Result", "Kind", "Summary")
    for item in actions:
        output.add_row(
            Text(item.id[:12], style="text.muted"),
            status_text(item.status),
            Text(item.result_kind or "", style="text.secondary"),
            Text(item.kind, style="tool"),
            Text(item.summary, style="text.secondary"),
        )
    console.print(output)


def render_sources(console: Console, sources: list[object]) -> None:
    if not sources:
        console.print("[text.secondary]No external sources in this session.[/]")
        return
    output = table("Source", "Title", "URL", "Flags")
    for item in sources:
        output.add_row(
            Text(item.id[:12], style="text.muted"),
            item.title[:60],
            Text(item.url, style="path"),
            Text("untrusted/injection" if item.suspicious else "untrusted", style="warning"),
        )
    console.print(output)


def render_memories(console: Console, memories: list[object], *, title: str) -> None:
    if not memories:
        console.print("[text.secondary]No matching memories.[/]")
        return
    output = table("Memory", "Status", "Type", "Scope", "Confidence", "Source", "Content")
    for item in memories:
        output.add_row(
            Text(item.id[:12], style="text.muted"),
            status_text(item.status.value),
            item.type.value,
            item.scope.value,
            f"{item.confidence:g}",
            item.source_kind,
            (item.content or "(purged)")[:80],
        )
    output.title = title
    console.print(output)


def render_memory(console: Console, item: object) -> None:
    details = Text.assemble(
        ("id=", "text.muted"), (f"{item.id}\n", "text.secondary"),
        ("type=", "text.muted"), (f"{item.type.value}\n", "text.secondary"),
        ("scope=", "text.muted"), (f"{item.scope.value}\n", "text.secondary"),
        ("status=", "text.muted"), (f"{item.status.value}\n", "text.secondary"),
        ("revision=", "text.muted"), (f"{item.revision}\n", "text.secondary"),
        ("confidence=", "text.muted"), (f"{item.confidence:g}\n", "text.secondary"),
        ("source=", "text.muted"), (f"{item.source_kind}:{item.source_ref or '-'}\n", "text.secondary"),
        ("expires_at=", "text.muted"), (f"{item.expires_at or 'never'}\n\n", "text.secondary"),
        (item.content or "(content permanently purged)", "text.primary"),
    )
    console.print(Panel(details, title=f"Memory {item.id[:12]}", border_style="border.focus"))


def render_memory_policy(console: Console, memory: object) -> None:
    console.print(
        Text.assemble(
            ("project_write_enabled=", "text.muted"),
            (f"{memory.project_write_enabled}\n", "success" if memory.project_write_enabled else "warning"),
            ("local_write_enabled=", "text.muted"),
            (f"{memory.local_write_enabled}\n", "success" if memory.local_write_enabled else "warning"),
            ("effective_write_enabled=", "text.muted"),
            (str(memory.write_enabled), "success" if memory.write_enabled else "warning"),
        )
    )


def render_status(console: Console, agent: object) -> None:
    console.print(
        Text.assemble(
            ("session=", "text.muted"),
            (f"{agent.session_id}\n", "text.secondary"),
            ("workspace=", "text.muted"),
            (f"{agent.workspace}\n", "path"),
            ("model=", "text.muted"),
            (f"{agent.model}\n", "text.secondary"),
            ("max_turns=", "text.muted"),
            (f"{agent.max_turns}\n", "text.secondary"),
            ("permission_mode=", "text.muted"),
            (agent.permission_mode.value, "waiting"),
        )
    )


def render_change_approval(console: Console, change: object) -> None:
    console.print(
        Panel(
            Text(change.diff or "(no textual diff)", style="code"),
            title=f"Apply {change.id[:12]} · {change.path}",
            border_style="warning",
        )
    )


def render_command_approval(console: Console, command: object) -> None:
    console.print(
        Panel(
            Text(" ".join(command.argv), style="command"),
            title=f"Run {command.id[:12]} · cwd={command.cwd} · timeout={command.timeout_seconds:g}s",
            border_style="warning",
        )
    )


def render_command_result(console: Console, result: object) -> None:
    console.print(f"[success]{result.status}:[/] [text.muted]exit_code={result.exit_code}[/]")
    if result.stdout:
        console.print(Panel(Text(result.stdout, style="tool"), title="stdout", border_style="border.muted"))
    if result.stderr:
        console.print(Panel(Text(result.stderr, style="error"), title="stderr", border_style="error"))


def render_undo_preview(console: Console, change: object, reverse: str) -> None:
    console.print(
        Panel(
            Text(reverse or "(remove file)", style="code"),
            title=f"Undo {change.id[:12]} · {change.path}",
            border_style="warning",
        )
    )


def render_git_diff(console: Console, output: object, *, success: bool) -> None:
    console.print(Text(str(output or ""), style="code" if success else "error"))


def render_external_approval(console: Console, action: object, payload: object) -> None:
    console.print(
        Panel(
            Text(str(payload), style="tool"),
            title=f"Approve {action.kind} · {action.id[:12]}",
            border_style="warning",
        )
    )


def render_pending_external_action(console: Console, action: object, payload: object) -> None:
    details = f"{action.summary}\n\nID: {action.id}\nPayload: {payload}"
    console.print(
        Panel(
            Text(details, style="tool"),
            title=f"Approval required · {action.kind}",
            border_style="warning",
        )
    )


def render_external_result(console: Console, result: object, safe_result: object) -> None:
    console.print(f"[success]{result.status}:[/] [text.muted]{result.id[:12]}[/] · [tool]{result.kind}[/]")
    if result.error:
        console.print(f"[error]{result.error}[/]")
    elif result.kind in {"web_search", "web_fetch"} and result.result:
        count = len(result.result.get("results", [])) if result.kind == "web_search" else 1
        console.print(
            f"[info]Saved {count} Web source(s);[/] "
            "[text.secondary]Agent will continue automatically.[/]"
        )
    elif result.result:
        console.print(
            Panel(
                Text(str(safe_result), style="tool"),
                title="Safe result summary",
                border_style="border.muted",
            )
        )


def render_mcp_servers(console: Console, servers: list[object]) -> None:
    output = table("Server", "Scope", "Enabled", "Allowed tools")
    for server in servers:
        output.add_row(
            Text(server.name, style="tool"),
            Text(server.scope, style="text.secondary"),
            Text(str(server.enabled), style="success" if server.enabled else "text.disabled"),
            Text(", ".join(server.allowed_tools), style="command"),
        )
    console.print(output)


def render_mcp_server(console: Console, server: object) -> None:
    console.print(
        Text.assemble(
            ("server=", "text.muted"),
            (f"{server.name}\n", "tool"),
            ("scope=", "text.muted"),
            (f"{server.scope}\n", "text.secondary"),
            ("cwd=", "text.muted"),
            (f"{server.cwd}\n", "path"),
            ("enabled=", "text.muted"),
            (f"{server.enabled}\n", "success" if server.enabled else "text.disabled"),
            ("allowed_tools=", "text.muted"),
            (", ".join(server.allowed_tools), "command"),
        )
    )


def render_session_list(console: Console, sessions: list[object]) -> None:
    output = table("Session", "Workspace", "Model", "Updated")
    for item in sessions:
        output.add_row(
            Text(item.id, style="text.muted"),
            Text(str(item.workspace), style="path"),
            Text(item.model, style="text.secondary"),
            Text(item.updated_at, style="text.muted"),
        )
    console.print(output)


def render_doctor(
    console: Console,
    *,
    workspace: object,
    database: object,
    model: str,
    endpoint: str,
    api_ready: bool,
    git_ready: bool,
    commands: list[str],
    memory_database: object | None = None,
    memory_fts: bool | None = None,
    memory_write_enabled: bool | None = None,
) -> None:
    output = table("Check", "Result")
    output.add_row("Workspace", Text(str(workspace), style="path"))
    output.add_row("SQLite", Text(str(database), style="path"))
    output.add_row("Model", Text(model, style="text.secondary"))
    output.add_row("Endpoint", Text(endpoint, style="path"))
    output.add_row("API key", Text("configured" if api_ready else "missing", style="success" if api_ready else "warning"))
    output.add_row(
        "Git",
        Text(
            "repository" if git_ready else "not a repository (Git tools will be unavailable)",
            style="success" if git_ready else "warning",
        ),
    )
    output.add_row("Command templates", Text(", ".join(commands) or "none detected", style="command"))
    if memory_database is not None:
        output.add_row("Memory SQLite", Text(str(memory_database), style="path"))
        output.add_row("Memory FTS5", Text("available" if memory_fts else "missing", style="success" if memory_fts else "error"))
        output.add_row("Memory writes", Text("enabled" if memory_write_enabled else "disabled", style="success" if memory_write_enabled else "warning"))
    console.print(output)


_startup_banner = startup_banner
_permission_badge = permission_badge
