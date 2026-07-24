"""Top-level typed settings aggregate and loading entry point."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain import LoopDetectionSettings
from .types import (
    AgentSettings,
    BudgetSettings,
    CommandSettings,
    ContextSettings,
    DocumentSettings,
    LspSettings,
    McpSettings,
    MemorySettings,
    ModelProfileSettings,
    ModelSettings,
    ProviderSettings,
    RoutingSettings,
    RuntimeSettings,
    ShellSettings,
    ToolSettings,
    WebSettings,
    WorktreeSettings,
)

if TYPE_CHECKING:
    from ..layout import ProjectLayout


@dataclass(frozen=True)
class Settings:
    model_config: ModelSettings
    runtime: RuntimeSettings
    tools: ToolSettings
    shell: ShellSettings
    context: ContextSettings
    command: CommandSettings
    web: WebSettings
    mcp: McpSettings
    permission_mode: str
    memory: MemorySettings = MemorySettings()
    agents: AgentSettings = AgentSettings()
    lsp: LspSettings = LspSettings()
    documents: DocumentSettings = DocumentSettings()
    worktree: WorktreeSettings = WorktreeSettings()
    providers: dict[str, ProviderSettings] | None = None
    models: dict[str, ModelProfileSettings] | None = None
    routing: RoutingSettings | None = None
    budget: BudgetSettings = BudgetSettings()
    loop_detection: LoopDetectionSettings = LoopDetectionSettings()

    @classmethod
    def load(
        cls, workspace: Path, *, layout: ProjectLayout | None = None
    ) -> "Settings":
        from ..layout import ProjectLayout
        from .loader import load_config_document
        from .resolver import resolve_settings

        resolved_layout = layout or ProjectLayout.discover(workspace)
        document: dict[str, object] = {}
        if resolved_layout.config.is_file():
            document = load_config_document(resolved_layout.config)
        return resolve_settings(
            document,
            layout=resolved_layout,
            settings_factory=cls,
        )
