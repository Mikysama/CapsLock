"""Workspace-scoped composition root and resource ownership."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings
from ..layout import ProjectLayout
from ..observability import EventSink
from ..permissions import PermissionMode
from ..runtime import WorkspaceAgent
from ..session import SessionStore
from ..storage import MemoryStore


class WorkspaceApplication:
    def __init__(self, *, workspace: Path, settings: Settings, client: Any, session_id: str | None = None, layout: ProjectLayout | None = None) -> None:
        self.workspace = workspace.resolve()
        self.layout = layout or ProjectLayout.discover(self.workspace)
        self.settings = settings
        self.client = client
        self.store = SessionStore(self.layout.database)
        try:
            memory_path = settings.memory.database or self.layout.user.memory
            self.memory_store = MemoryStore(memory_path)
            mode = PermissionMode.parse(self.store.workspace_setting("permission_mode", settings.permission_mode) or settings.permission_mode)
            self.agent = WorkspaceAgent(
                client,
                workspace=self.workspace,
                model=settings.model,
                store=self.store,
                session_id=session_id,
                max_turns=settings.runtime.max_turns,
                max_context_messages=settings.runtime.max_context_messages,
                command_timeout_seconds=settings.command.command_timeout_seconds,
                command_output_bytes=settings.command.command_output_bytes,
                input_cost_per_million=settings.model_config.input_cost_per_million,
                output_cost_per_million=settings.model_config.output_cost_per_million,
                tavily_api_key=settings.web.tavily_api_key,
                web_timeout_seconds=settings.web.web_timeout_seconds,
                web_max_bytes=settings.web.web_max_bytes,
                web_max_redirects=settings.web.web_max_redirects,
                mcp_timeout_seconds=settings.mcp.mcp_timeout_seconds,
                mcp_output_bytes=settings.mcp.mcp_output_bytes,
                permission_mode=mode,
                event_sink=EventSink(self.layout.events),
                memory_store=self.memory_store,
                memory_project_write_enabled=settings.memory.project_write_enabled,
                layout=self.layout,
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            close = getattr(self.client, "close", None)
            if callable(close):
                close()
        finally:
            try:
                self.store.close()
            finally:
                memory_store = getattr(self, "memory_store", None)
                if memory_store is not None:
                    memory_store.close()

    def __enter__(self) -> "WorkspaceApplication":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
