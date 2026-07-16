"""Rendering and confirmation for explicit layout migration."""

from __future__ import annotations

import sys

from rich.console import Console

from ..layout import LayoutConflict, LayoutMigrator, MigrationPlan, ProjectLayout


def run_layout_migration(
    console: Console,
    layout: ProjectLayout,
    *,
    scope: str,
    apply: bool,
    yes: bool,
) -> int:
    migrator = LayoutMigrator(layout)
    try:
        plan = migrator.plan(scope)
    except (LayoutConflict, ValueError) as exc:
        console.print(f"[error]Error:[/] {exc}")
        return 2
    except OSError as exc:
        console.print(f"[error]Migration I/O error:[/] {exc}")
        return 1

    _render_plan(console, plan, dry_run=not apply)
    if plan.conflicts:
        console.print("[error]Migration blocked by conflicts; no files were changed.[/]")
        return 2
    if not apply:
        return 0
    if not plan.changes:
        console.print("[success]Layout is already migrated.[/]")
        return 0
    if not yes:
        if not sys.stdin.isatty():
            console.print("[error]Error:[/] non-interactive migration requires --apply --yes")
            return 2
        answer = console.input("Apply this layout migration? [y/N] ").strip().casefold()
        if answer not in {"y", "yes"}:
            console.print("[warning]Migration cancelled; no files were changed.[/]")
            return 2
    try:
        migrator.apply(plan)
    except LayoutConflict as exc:
        console.print(f"[error]Error:[/] {exc}")
        return 2
    except OSError as exc:
        console.print(f"[error]Migration I/O error:[/] {exc}")
        return 1
    console.print("[success]Layout migration completed and verified.[/]")
    return 0


def _render_plan(console: Console, plan: MigrationPlan, *, dry_run: bool) -> None:
    mode = "dry-run" if dry_run else "apply"
    console.print(f"[info]Layout migration:[/] scope={plan.scope} mode={mode}")
    for item in plan.items:
        detail = f" ({item.detail})" if item.detail else ""
        console.print(
            f"  [{_status_style(item.status)}]{item.status:8}[/] "
            f"{item.kind}: [path]{item.source}[/] -> [path]{item.destination}[/]{detail}"
        )


def _status_style(status: str) -> str:
    if status == "conflict":
        return "error"
    if status == "missing":
        return "text.muted"
    if status == "cleanup":
        return "warning"
    return "success"
