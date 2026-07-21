"""CLI presentation for lifecycle backup and portable transfer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rich.console import Console

from ..layout import ProjectLayout
from ..lifecycle import BACKUP_FORMAT, LifecycleService
from ..storage.memory_v2 import MemoryRepositories
from ..storage.repositories_v2 import WorkspaceRepositories


async def backup_command(console: Console, layout: ProjectLayout, args) -> int:
    service = LifecycleService(layout)
    operation = args.backup_command or "list"
    if operation == "create":
        path = await asyncio.to_thread(service.backup_create, args.destination)
        console.print(f"[success]Backup created:[/] {path}")
        return 0
    if operation == "list":
        for path in service.backup_list():
            console.print(str(path))
        return 0
    if operation == "verify":
        manifest = await asyncio.to_thread(
            service.verify, args.archive, expected_format=BACKUP_FORMAT
        )
        _manifest(console, manifest)
        return 0
    if not args.yes:
        answer = await asyncio.to_thread(
            console.input,
            "Restore replaces workspace state and the complete user memory database. Continue? [y/N] ",
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            return 0
    safety = await asyncio.to_thread(service.backup_restore, args.archive)
    console.print(f"[success]Backup restored. Previous state:[/] {safety}")
    return 0


async def export_lifecycle(
    console: Console,
    layout: ProjectLayout,
    destination: Path,
    *,
    include_global_memory: bool,
) -> int:
    service = LifecycleService(layout)
    path = await asyncio.to_thread(
        service.export, destination, include_global_memory=include_global_memory
    )
    console.print(f"[success]Portable export created:[/] {path}")
    return 0


async def import_lifecycle(
    console: Console, layout: ProjectLayout, archive: Path, *, yes: bool
) -> int:
    if not yes:
        answer = await asyncio.to_thread(
            console.input,
            "Merge this archive into the current workspace? Imported actions require review. [y/N] ",
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            return 0
    # Opening through the repository layer applies the supported v1.8 schema migration first.
    workspace = await WorkspaceRepositories.open(
        layout.database, workspace=layout.workspace
    )
    memory = await MemoryRepositories.open(layout.user.memory)
    await workspace.close()
    await memory.close()
    report = await asyncio.to_thread(LifecycleService(layout).import_archive, archive)
    console.print_json(json.dumps(report, ensure_ascii=False))
    return 0


def _manifest(console: Console, manifest: dict[str, object]) -> None:
    console.print(
        f"[success]Valid {manifest.get('format')} v{manifest.get('version')}[/] "
        f"from CapsLock {manifest.get('source_version')} with {len(manifest.get('files', {}))} files"
    )
