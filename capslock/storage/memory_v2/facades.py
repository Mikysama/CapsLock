"""Narrow memory repository views over the compatibility aggregate."""

from __future__ import annotations

from typing import Any

from .lifecycle import MemoryRepository


class MemoryLifecycleRepository:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def create(self, **values: Any):
        return await self.repository.create(**values)

    async def edit(self, memory_id: str, **values: Any):
        return await self.repository.edit(memory_id, **values)

    async def forget(self, memory_id: str):
        return await self.repository.forget(memory_id)

    async def undo(self, memory_id: str):
        return await self.repository.undo(memory_id)

    async def purge(self, memory_id: str):
        return await self.repository.purge(memory_id)

    async def require(self, memory_id: str, **values: Any):
        return await self.repository.require(memory_id, **values)

    async def purge_session(self, **values: Any) -> int:
        return await self.repository.purge_session(**values)


class MemoryQueryRepository:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def get(self, memory_id: str, **values: Any):
        return await self.repository.get(memory_id, **values)

    async def require(self, memory_id: str, **values: Any):
        return await self.repository.require(memory_id, **values)

    async def resolve(self, prefix: str, **values: Any):
        return await self.repository.resolve(prefix, **values)

    async def list_visible(self, **values: Any):
        return await self.repository.list_visible(**values)

    async def search_ranked(self, query: str, **values: Any):
        return await self.repository.search_ranked(query, **values)

    async def search(self, query: str, **values: Any):
        return await self.repository.search(query, **values)


class MemorySourceRepository:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def record_access(self, memories, **values: Any) -> None:
        await self.repository.record_access(memories, **values)

    async def excluded_runs(self, **values: Any) -> set[str]:
        return await self.repository.excluded_runs(**values)

    async def invalidate(self, memory_id: str, **values: Any) -> None:
        await self.repository.invalidate_source(memory_id, **values)

    async def list(self, memory_id: str) -> list[dict[str, object]]:
        return await self.repository.sources(memory_id)

    async def add(self, memory_id: str, **values: Any) -> None:
        await self.repository.add_source(memory_id, **values)


class MemorySettingsRepository:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def get(self, workspace: str) -> dict[str, object]:
        return await self.repository.settings(workspace)

    async def set(self, workspace: str, name: str, value: object) -> None:
        await self.repository.set_setting(workspace, name, value)


class MemoryAuditRepository:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def record_export(self, **values: Any) -> None:
        await self.repository.audit_export(**values)
