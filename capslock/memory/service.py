"""Async facade over focused v2 memory services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain import (
    EmbeddingBackend,
    MemoryCandidateStatus,
    MemoryInfo,
    MemoryOrigin,
    MemoryPolicy,
    MemoryScope,
    MemoryType,
)
from ..layout import UserLayout
from ..storage.memory_v2 import MemoryRepositories, workspace_key
from .candidates import CandidateService, MemoryExtractionResult
from .embeddings import (
    EmbeddingService,
    ExternalEmbeddingConfig,
)
from .embedding_policy import EmbeddingPolicyService
from .recall import RecallService
from .transfer import MemoryTransferService
from .validation import confidence, expiry, validated_text


@dataclass(frozen=True)
class MemorySettingsView:
    project_write_enabled: bool
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
        return self.project_write_enabled and self.local_write_enabled


def default_memory_database() -> Path:
    return UserLayout.from_environment().canonical_memory


class MemoryService:
    def __init__(
        self,
        repositories: MemoryRepositories,
        *,
        workspace: Path,
        session_id: str,
        project_write_enabled: bool = True,
        event=None,
        embedding_provider_factory: Any = None,
        external_embedding_profiles: dict[str, ExternalEmbeddingConfig] | None = None,
        source_validator=None,
    ) -> None:
        self.repositories = repositories
        self.workspace = workspace.resolve()
        self.workspace_key = workspace_key(self.workspace)
        self.session_id = session_id
        self.project_write_enabled = project_write_enabled
        self.event = event or (lambda *args, **kwargs: None)
        self.external_embedding_profiles = external_embedding_profiles or {}
        self.embeddings = EmbeddingService(
            repositories,
            workspace=self.workspace_key,
            session_id=session_id,
            cache_dir=UserLayout.from_environment().home / "cache" / "fastembed",
            provider_factory=embedding_provider_factory,
            external_profiles=self.external_embedding_profiles,
        )
        self.recall_service = RecallService(
            repositories,
            self.embeddings,
            workspace=self.workspace_key,
            session_id=session_id,
            event=self.event,
            source_validator=source_validator,
        )
        self.candidate_service = CandidateService(
            repositories,
            self.embeddings,
            workspace=self.workspace_key,
            session_id=session_id,
            event=self.event,
        )
        self.transfer = MemoryTransferService(
            repositories,
            workspace=self.workspace,
            workspace_key=self.workspace_key,
            session_id=session_id,
            event=self.event,
        )
        self.embedding_policy = EmbeddingPolicyService(
            repositories,
            workspace=self.workspace_key,
            profiles=self.external_embedding_profiles,
            list_memories=lambda: self.list(limit=-1),
            event=self.event,
        )

    async def settings(self) -> MemorySettingsView:
        raw = await self.repositories.settings.get(self.workspace_key)
        return MemorySettingsView(
            self.project_write_enabled,
            bool(raw["write_enabled"]),
            raw["policy"],
            bool(raw["recall_enabled"]),
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

    async def add(
        self,
        *,
        content: str,
        memory_type: MemoryType,
        scope: MemoryScope,
        confidence: float = 1.0,
        expires_at: str | None = None,
    ) -> tuple[MemoryInfo, tuple[str, ...]]:
        await self._require_write()
        safe, rules = validated_text(content)
        workspace, session_id = self._scope_keys(scope)
        item = await self.repositories.lifecycle.create(
            content=safe,
            memory_type=memory_type,
            scope=scope,
            workspace=workspace,
            session_id=session_id,
            source_kind="manual",
            source_ref=self.session_id,
            confidence=confidence_value(confidence),
            expires_at=expiry(expires_at),
            origin=MemoryOrigin.MANUAL,
            run_id=self.session_id,
        )
        await self._index(item)
        self.event(
            "memory_added",
            memory_id=item.id,
            scope=item.scope.value,
            revision=item.revision,
        )
        return item, rules

    async def edit(
        self,
        prefix: str,
        *,
        content: str,
        memory_type: MemoryType,
        confidence: float,
        expires_at: str | None,
    ) -> tuple[MemoryInfo, tuple[str, ...]]:
        await self._require_write()
        current = await self.resolve(prefix)
        safe, rules = validated_text(content)
        item = await self.repositories.lifecycle.edit(
            current.id,
            content=safe,
            memory_type=memory_type,
            source_kind="manual",
            source_ref=self.session_id,
            confidence=confidence_value(confidence),
            expires_at=expiry(expires_at),
        )
        await self._index(item)
        self.event(
            "memory_edited",
            memory_id=item.id,
            scope=item.scope.value,
            revision=item.revision,
        )
        return item, rules

    async def forget(self, prefix: str) -> MemoryInfo:
        await self._require_write()
        item = await self.repositories.lifecycle.forget((await self.resolve(prefix)).id)
        self.event("memory_forgotten", memory_id=item.id)
        return item

    async def undo(self, prefix: str) -> MemoryInfo:
        await self._require_write()
        item = await self.repositories.lifecycle.undo((await self.resolve(prefix)).id)
        await self._index(item)
        self.event("memory_undone", memory_id=item.id)
        return item

    async def purge(self, prefix: str) -> MemoryInfo:
        await self._require_write()
        item = await self.repositories.lifecycle.purge((await self.resolve(prefix)).id)
        self.event("memory_purged", memory_id=item.id)
        return item

    async def resolve(
        self, prefix: str, *, include_inactive: bool = True
    ) -> MemoryInfo:
        return await self.repositories.query.resolve(
            prefix,
            workspace=self.workspace_key,
            session_id=self.session_id,
            include_inactive=include_inactive,
        )

    async def list(
        self,
        *,
        scope: MemoryScope | None = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> list[MemoryInfo]:
        return await self.repositories.query.list_visible(
            workspace=self.workspace_key,
            session_id=self.session_id,
            scope=scope,
            include_inactive=include_inactive,
            limit=limit,
        )

    async def search(
        self, query: str, *, run_id: str | None = None, limit: int = 10
    ) -> list[MemoryInfo]:
        normalized = query.strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("memory search query must contain 1-512 characters")
        items = await self.repositories.query.search(
            normalized,
            workspace=self.workspace_key,
            session_id=self.session_id,
            limit=max(1, min(limit, 20)),
        )
        if run_id and items:
            await self.repositories.sources.record_access(
                items,
                workspace=self.workspace_key,
                session_id=self.session_id,
                run_id=run_id,
            )
        return items

    async def get_for_model(self, prefix: str, *, run_id: str) -> MemoryInfo:
        item = await self.resolve(prefix, include_inactive=False)
        await self.repositories.sources.record_access(
            [item],
            workspace=self.workspace_key,
            session_id=self.session_id,
            run_id=run_id,
        )
        return item

    async def excluded_runs(self) -> set[str]:
        return await self.repositories.sources.excluded_runs(
            workspace=self.workspace_key, session_id=self.session_id
        )

    async def recall_context(self, query: str, *, run_id: str):
        return await self.recall_service.context(query, run_id=run_id)

    async def context(self, run_id: str | None = None):
        return await self.repositories.recalls.hits(
            workspace=self.workspace_key, session_id=self.session_id, run_id=run_id
        )

    async def capture_candidates(self, chat_model, **kwargs) -> MemoryExtractionResult:
        return await self.candidate_service.capture(
            chat_model, write_enabled=(await self.settings()).write_enabled, **kwargs
        )

    async def candidates(self, *, include_all: bool = False):
        return await self.repositories.candidates.list(
            workspace=self.workspace_key,
            session_id=self.session_id,
            include_all=include_all,
        )

    async def resolve_candidate(self, prefix: str):
        return await self.repositories.candidates.resolve(
            prefix, workspace=self.workspace_key, session_id=self.session_id
        )

    async def accept_candidate(self, prefix: str, **kwargs):
        await self._require_write()
        return await self.candidate_service.accept(
            await self.resolve_candidate(prefix), **kwargs
        )

    async def reject_candidate(self, prefix: str):
        await self._require_write()
        item = await self.resolve_candidate(prefix)
        return await self.repositories.candidates.decide(
            item.id, MemoryCandidateStatus.REJECTED
        )

    async def purge_candidate(self, prefix: str):
        await self._require_write()
        return await self.repositories.candidates.purge(
            (await self.resolve_candidate(prefix)).id
        )

    async def cleanup(self) -> dict[str, int]:
        await self._require_write()
        count = await self.repositories.candidates.cleanup(workspace=self.workspace_key)
        result = {"candidate_contents": count}
        self.event("memory_cleanup_completed", **result)
        return result

    async def rebuild_embeddings(self) -> tuple[int, int]:
        return await self.embeddings.rebuild(await self.list(limit=-1))

    async def export_json(self, *args, **kwargs):
        return await self.transfer.export_json(*args, **kwargs)

    async def import_json(self, *args, **kwargs):
        await self._require_write()
        return await self.transfer.import_json(*args, **kwargs)

    async def _index(self, item: MemoryInfo) -> None:
        try:
            await self.embeddings.index(item)
        except Exception as exc:
            self.event(
                "memory_embedding_failed", operation="index", error=type(exc).__name__
            )

    async def _require_write(self) -> None:
        view = await self.settings()
        if not view.project_write_enabled:
            raise PermissionError("memory writes are disabled by project config")
        if not view.local_write_enabled:
            raise PermissionError("memory writes are disabled locally")

    def _scope_keys(self, scope: MemoryScope) -> tuple[str | None, str | None]:
        if scope is MemoryScope.GLOBAL:
            return None, None
        if scope is MemoryScope.WORKSPACE:
            return self.workspace_key, None
        return self.workspace_key, self.session_id


confidence_value = confidence
