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
    McpSettings,
    MemorySettings,
    ModelProfileSettings,
    ModelSettings,
    ProviderSettings,
    RoutingSettings,
    RuntimeSettings,
    WebSettings,
)

if TYPE_CHECKING:
    from ..layout import ProjectLayout


@dataclass(frozen=True)
class Settings:
    model_config: ModelSettings
    runtime: RuntimeSettings
    command: CommandSettings
    web: WebSettings
    mcp: McpSettings
    permission_mode: str
    memory: MemorySettings = MemorySettings()
    agents: AgentSettings = AgentSettings()
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
            document = load_config_document(resolved_layout.config, migrate=True)
        return resolve_settings(
            document,
            layout=resolved_layout,
            settings_factory=cls,
        )
