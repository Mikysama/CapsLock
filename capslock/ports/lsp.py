"""Neutral Language Server Protocol client boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..policy import WorkspacePolicy


class LspClientPort(Protocol):
    @property
    def available(self) -> bool: ...

    def supports(self, path: Path) -> bool: ...

    async def request(
        self, path_text: str, method: str, params: dict[str, object]
    ) -> object: ...

    async def did_change(self, path_text: str | Path) -> None: ...

    async def switch_policy(self, policy: WorkspacePolicy) -> None: ...

    async def close(self) -> None: ...


__all__ = ["LspClientPort"]
