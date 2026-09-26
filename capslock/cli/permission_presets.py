"""Shared permission-preset interaction for both terminal frontends."""

import asyncio
import time

from .command_handlers.support import get_ui
from ..tooling.permission_policy.presets import write_workspace_edit_preset


async def workspace_edit_preset(context, *, enabled: bool) -> None:
    engine = context.session.permission_engine
    path = dict(engine.paths).get("local") if engine else None
    if path is None:
        raise ValueError("workspace-local permissions are unavailable")
    detail = (
        "Allow create_file, edit_file, write_file and edit_notebook within this workspace. "
        "Existing path restrictions and deny/ask rules still apply. "
        "Shell, network, external paths, deletion, MCP and plugins receive no new permission."
        if enabled
        else "Remove only the workspace-edit preset rules; keep all other rules."
    )
    started = time.monotonic()
    approved = await get_ui(context).confirm(
        "Enable workspace-edit" if enabled else "Remove workspace-edit",
        detail,
        default=False,
    )
    events = getattr(context.session, "events", None)
    if events is not None:
        events.emit(
            "permission_preset_review",
            approved=approved,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    if approved:
        await asyncio.to_thread(write_workspace_edit_preset, path, enabled=enabled)
