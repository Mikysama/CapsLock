"""Embedding backend policy, consent, and cache invalidation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable

from ..domain import EmbeddingBackend, MemoryInfo
from ..storage.memory_v2 import MemoryRepositories
from .embeddings import (
    DEFAULT_FASTEMBED_MODEL,
    ExternalEmbeddingConfig,
    validate_loopback_endpoint,
)


class EmbeddingPolicyService:
    def __init__(
        self,
        repositories: MemoryRepositories,
        *,
        workspace: str,
        profiles: dict[str, ExternalEmbeddingConfig],
        list_memories: Callable[[], Awaitable[list[MemoryInfo]]],
        event,
    ) -> None:
        self.repositories = repositories
        self.workspace = workspace
        self.profiles = profiles
        self.list_memories = list_memories
        self.event = event

    async def configure(
        self,
        backend: EmbeddingBackend,
        *,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        if backend is EmbeddingBackend.EXTERNAL:
            raise ValueError("external embeddings require explicit preview and consent")
        if backend is EmbeddingBackend.LOCAL_HTTP:
            if not endpoint:
                raise ValueError("local-http embeddings require an endpoint")
            endpoint = validate_loopback_endpoint(endpoint)
            if not model:
                raise ValueError("local-http embeddings require a model")
        else:
            endpoint = None
        if backend is EmbeddingBackend.FASTEMBED:
            model = model or DEFAULT_FASTEMBED_MODEL
        await self.repositories.embedding_audit.revoke(self.workspace)
        await self.repositories.settings.set(
            self.workspace, "embedding_backend", backend.value
        )
        await self.repositories.settings.set(self.workspace, "embedding_model", model)
        await self.repositories.settings.set(
            self.workspace, "embedding_endpoint", endpoint
        )
        for name in (
            "embedding_provider",
            "embedding_data_policy",
            "embedding_consent_id",
        ):
            await self.repositories.settings.set(self.workspace, name, None)
        await self.repositories.embeddings.clear(workspace=self.workspace)
        self.event(
            "memory_embedding_policy_changed", backend=backend.value, model=model
        )

    async def preview(self, profile: str) -> dict[str, object]:
        config = self.profiles.get(profile)
        if config is None:
            raise ValueError(f"unknown external embedding profile: {profile}")
        contents = [item.content for item in await self.list_memories() if item.content]
        payload: dict[str, object] = {
            "profile": profile,
            "provider": config.provider,
            "model": config.model,
            "data_policy": config.data_policy,
            "fields": ("memory.content", "recall.query"),
            "scopes": ("global", "workspace", "session"),
            "record_count": len(contents),
            "byte_count": sum(len(item.encode("utf-8")) for item in contents),
        }
        payload["content_hash"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return payload

    async def enable(self, profile: str, preview: dict[str, object]) -> None:
        current = await self.preview(profile)
        if current != preview:
            raise ValueError("external embedding preview changed; review it again")
        config = self.profiles[profile]
        consent_id = await self.repositories.embedding_audit.consent(
            workspace=self.workspace,
            provider=config.provider,
            model=config.model,
            data_policy=config.data_policy,
            fields=tuple(str(item) for item in current["fields"])
            + tuple(f"scope.{item}" for item in current["scopes"]),
            record_count=int(current["record_count"]),
            byte_count=int(current["byte_count"]),
            content_hash=str(current["content_hash"]),
        )
        values = {
            "embedding_backend": EmbeddingBackend.EXTERNAL.value,
            "embedding_model": profile,
            "embedding_endpoint": None,
            "embedding_provider": config.provider,
            "embedding_data_policy": config.data_policy,
            "embedding_consent_id": consent_id,
        }
        for name, value in values.items():
            await self.repositories.settings.set(self.workspace, name, value)
        await self.repositories.embeddings.clear(workspace=self.workspace)
        self.event(
            "memory_embedding_policy_changed",
            backend=EmbeddingBackend.EXTERNAL.value,
            profile=profile,
            provider=config.provider,
            consent_id=consent_id,
        )
