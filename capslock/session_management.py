"""Async v2 session export and deletion."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .security import redact
from .storage.memory_repositories import MemoryRepositories, workspace_key
from .storage.repositories import WorkspaceRepositories

SESSION_EXPORT_FORMAT = "capslock-session-export"
SESSION_EXPORT_VERSION = 3


class SessionManager:
    def __init__(
        self,
        repositories: WorkspaceRepositories,
        *,
        workspace: Path,
        memory_repositories: MemoryRepositories | None = None,
    ) -> None:
        self.repositories = repositories
        self.workspace = workspace.resolve()
        self.memory_repositories = memory_repositories

    async def export(self, session_id: str, destination: str | Path) -> Path:
        session = await self.repositories.sessions.require(session_id)
        target = self._export_path(destination)
        if target.exists():
            raise FileExistsError(f"export destination already exists: {target}")
        snapshot = await self.repositories.snapshots.session(session_id)
        document = redact(
            {
                "format": SESSION_EXPORT_FORMAT,
                "version": SESSION_EXPORT_VERSION,
                "exported_at": datetime.now(UTC).isoformat(),
                "session": {
                    **asdict(session),
                    "workspace": str(session.workspace),
                    "title_source": session.title_source.value,
                },
                **snapshot,
                "cost": dict(
                    zip(
                        ("input_tokens", "output_tokens", "cost_usd"),
                        await self.repositories.runs.session_cost(session_id),
                        strict=True,
                    )
                ),
            }
        )
        await asyncio.to_thread(self._write, target, document)
        return target

    async def delete(self, session_id: str) -> int:
        await self.repositories.sessions.require(session_id)
        if await self.repositories.sessions.has_active_work(session_id):
            raise ValueError(
                "active or approval-waiting sessions must be cancelled before deletion"
            )
        purged = 0
        if self.memory_repositories is not None:
            purged = await self.memory_repositories.lifecycle.purge_session(
                workspace=workspace_key(self.workspace), session_id=session_id
            )
        await self.repositories.sessions.delete(session_id)
        return purged

    def _export_path(self, destination: str | Path) -> Path:
        candidate = Path(destination)
        if candidate.is_absolute():
            raise ValueError("session export destination must be workspace-relative")
        target = (self.workspace / candidate).resolve()
        if not target.is_relative_to(self.workspace):
            raise ValueError("session export destination escapes the workspace")
        current = self.workspace
        for part in candidate.parts:
            if part in {"", "."}:
                continue
            current /= part
            if current.exists() and current.is_symlink():
                raise ValueError("session export does not follow symbolic links")
        return target

    @staticmethod
    def _write(target: Path, document: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
        try:
            (temporary / "session.json").write_text(
                json.dumps(document, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            (temporary / "session.md").write_text(_markdown(document), encoding="utf-8")
            os.replace(temporary, target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def _markdown(document: dict[str, Any]) -> str:
    session = document["session"]
    lines = [
        f"# {session['title']}",
        "",
        f"- Session: `{session['id']}`",
        f"- Workspace: `{session['workspace']}`",
        f"- Exported: {document['exported_at']}",
        "",
        "## Conversation",
        "",
    ]
    for item in document.get("messages", []):
        lines.extend((f"### {str(item['role']).title()}", "", str(item["content"]), ""))
    stopped = [item for item in document.get("runs", []) if item.get("stop_reason")]
    if stopped:
        lines.extend(("## Incomplete runs", ""))
        for item in stopped:
            lines.append(f"- `{str(item['id'])[:12]}` stopped: `{item['stop_reason']}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
