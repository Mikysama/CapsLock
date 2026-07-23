"""Workspace memory settings persistence."""

from __future__ import annotations

from ...domain import EmbeddingBackend, MemoryPolicy
from .core import Repository


class MemorySettingsRepository(Repository):
    async def get(self, workspace: str) -> dict[str, object]:
        await self.execute(
            "INSERT OR IGNORE INTO memory_workspace_settings(workspace_key) VALUES(?)",
            (workspace,),
        )
        row = await self.one(
            "SELECT * FROM memory_workspace_settings WHERE workspace_key=?",
            (workspace,),
        )
        assert row is not None
        return {
            "write_enabled": bool(row["write_enabled"]),
            "policy": MemoryPolicy(row["policy"]),
            "recall_enabled": bool(row["recall_enabled"]),
            "embedding_backend": EmbeddingBackend(row["embedding_backend"]),
            "embedding_model": row["embedding_model"],
            "embedding_endpoint": row["embedding_endpoint"],
            "embedding_provider": row["embedding_provider"],
            "embedding_data_policy": row["embedding_data_policy"],
            "embedding_consent_id": row["embedding_consent_id"],
        }

    async def set(self, workspace: str, name: str, value: object) -> None:
        allowed = {
            "write_enabled",
            "policy",
            "recall_enabled",
            "embedding_backend",
            "embedding_model",
            "embedding_endpoint",
            "embedding_provider",
            "embedding_data_policy",
            "embedding_consent_id",
        }
        if name not in allowed:
            raise ValueError("unsupported memory setting")
        await self.execute(
            "INSERT OR IGNORE INTO memory_workspace_settings(workspace_key) VALUES(?)",
            (workspace,),
        )
        await self.execute(
            f"UPDATE memory_workspace_settings SET {name}=? WHERE workspace_key=?",
            (value, workspace),
        )
