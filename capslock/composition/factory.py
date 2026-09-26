"""Workspace application composition shared by local entry points."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..bootstrap import WorkspaceApplication
from ..layout import ProjectLayout
from .providers import create_provider_clients

if TYPE_CHECKING:
    from ..configuration import Settings


async def create_application(
    workspace: Path,
    settings: Settings,
    session_id: str | None = None,
    *,
    layout: ProjectLayout | None = None,
) -> WorkspaceApplication:
    """Compose one workspace application for local clients."""
    return await WorkspaceApplication.open(
        workspace=workspace,
        settings=settings,
        client=create_provider_clients(settings),
        session_id=session_id,
        layout=layout,
    )


__all__ = ["create_application"]
