"""Memory and Skill ports."""

from __future__ import annotations

from typing import Any, Protocol

from ..domain import MemoryInfo, MemoryScope


class MemoryPort(Protocol):
    async def search(
        self, query: str, *, run_id: str | None = None, limit: int = 10
    ) -> list[MemoryInfo]: ...
    async def get_for_model(self, prefix: str, *, run_id: str) -> MemoryInfo: ...
    async def recall_context(self, query: str, *, run_id: str) -> Any: ...
    async def capture_candidates(self, chat_model: Any, **kwargs: Any) -> Any: ...
    async def list(
        self,
        *,
        scope: MemoryScope | None = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> list[MemoryInfo]: ...


class SkillPort(Protocol):
    def load(self, run_id: str, name: str, *, trigger: str) -> Any: ...
    def load_data(
        self, run_id: str, name: str, *, trigger: str
    ) -> tuple[dict[str, Any], Any]: ...
    def read_resource(
        self, run_id: str, name: str, path: str, **kwargs: Any
    ) -> Any: ...
    def finish_run(self, run_id: str) -> None: ...


class SkillRegistryPort(Protocol):
    def catalog(self) -> Any: ...
    def entries(self) -> list[Any]: ...
