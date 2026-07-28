"""Memory settings and embedding-policy coordination."""

from __future__ import annotations

from dataclasses import dataclass
from ..domain import EmbeddingBackend, MemoryPolicy


@dataclass(frozen=True)
class MemorySettingsView:
    manual_write_enabled: bool
    capture_enabled: bool
    project_recall_enabled: bool
    maintenance_enabled: bool
    local_write_enabled: bool
    policy: MemoryPolicy
    recall_enabled: bool
    embedding_backend: EmbeddingBackend
    embedding_model: str | None
    embedding_endpoint: str | None
    embedding_provider: str | None = None
    embedding_data_policy: str | None = None
    embedding_consent_id: int | None = None

    @property
    def write_enabled(self) -> bool:
        return self.manual_write_enabled and self.local_write_enabled


class MemorySettingsService:
    def __init__(
        self,
        *,
        repositories,
        workspace_key: str,
        manual_write_enabled: bool,
        capture_enabled: bool,
        recall_enabled: bool,
        maintenance_enabled: bool,
        capture_policy: MemoryPolicy,
        embedding_policy,
        event,
    ) -> None:
        self.repositories = repositories
        self.workspace_key = workspace_key
        self.manual_write_enabled = manual_write_enabled
        self.capture_enabled = capture_enabled
        self.project_recall_enabled = recall_enabled
        self.maintenance_enabled = maintenance_enabled
        self.project_capture_policy = capture_policy
        self.embedding_policy = embedding_policy
        self.event = event

    async def settings(self) -> MemorySettingsView:
        raw = await self.repositories.settings.get(self.workspace_key)
        policies = {
            MemoryPolicy.OFF: 0,
            MemoryPolicy.REVIEW: 1,
            MemoryPolicy.AUTOMATIC: 2,
        }
        effective_policy = min(
            (self.project_capture_policy, raw["policy"]), key=policies.__getitem__
        )
        return MemorySettingsView(
            self.manual_write_enabled,
            self.capture_enabled and bool(raw["capture_enabled"]),
            self.project_recall_enabled,
            self.maintenance_enabled and bool(raw["maintenance_enabled"]),
            bool(raw["write_enabled"]),
            effective_policy,
            self.project_recall_enabled and bool(raw["recall_enabled"]),
            raw["embedding_backend"],
            raw["embedding_model"],
            raw["embedding_endpoint"],
            raw["embedding_provider"],
            raw["embedding_data_policy"],
            raw["embedding_consent_id"],
        )

    async def set_local_write_enabled(self, enabled: bool) -> None:
        await self.repositories.settings.set(
            self.workspace_key, "write_enabled", int(enabled)
        )

    async def set_capture_enabled(self, enabled: bool) -> None:
        await self.repositories.settings.set(
            self.workspace_key, "capture_enabled", int(enabled)
        )
        self.event("memory_capture_policy_changed", enabled=enabled)
        self.event(
            "memory_policy_changed",
            enabled=enabled,
            effective=(await self.settings()).write_enabled,
        )

    async def set_policy(self, policy: MemoryPolicy) -> None:
        await self.repositories.settings.set(self.workspace_key, "policy", policy.value)
        self.event("memory_capture_policy_changed", policy=policy.value)

    async def set_recall_enabled(self, enabled: bool) -> None:
        await self.repositories.settings.set(
            self.workspace_key, "recall_enabled", int(enabled)
        )
        self.event("memory_recall_policy_changed", enabled=enabled)

    async def set_manual_write_enabled(self, enabled: bool) -> None:
        await self.set_local_write_enabled(enabled)

    async def set_maintenance_enabled(self, enabled: bool) -> None:
        await self.repositories.settings.set(
            self.workspace_key, "maintenance_enabled", int(enabled)
        )

    async def configure_embeddings(
        self,
        backend: EmbeddingBackend,
        *,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        await self.embedding_policy.configure(backend, model=model, endpoint=endpoint)

    async def external_embedding_preview(self, profile: str) -> dict[str, object]:
        return await self.embedding_policy.preview(profile)

    async def enable_external_embeddings(
        self, profile: str, preview: dict[str, object]
    ) -> None:
        await self.embedding_policy.enable(profile, preview)


__all__ = ["MemorySettingsService", "MemorySettingsView"]
