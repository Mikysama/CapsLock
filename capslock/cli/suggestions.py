"""Unified prompt suggestions and a bounded workspace file index."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Suggestion:
    value: str
    description: str
    kind: str


class SuggestionProvider(Protocol):
    def suggestions(self, prefix: str) -> Iterable[Suggestion]: ...


class StaticSuggestionProvider:
    def __init__(
        self,
        marker: str,
        kind: str,
        values: Callable[[], Iterable[tuple[str, str]]],
    ) -> None:
        self.marker, self.kind, self.values = marker, kind, values

    def suggestions(self, prefix: str) -> Iterable[Suggestion]:
        if not prefix.startswith(self.marker):
            return ()
        folded = prefix.casefold()
        return tuple(
            Suggestion(f"{self.marker}{name}", description, self.kind)
            for name, description in self.values()
            if f"{self.marker}{name}".casefold().startswith(folded)
        )


class WorkspaceFileSuggestionProvider:
    def __init__(self, workspace: Path, *, limit: int = 100) -> None:
        self.workspace = workspace.resolve()
        self.limit = limit
        self._signature: tuple[int, int] | None = None
        self._paths: tuple[str, ...] = ()

    def suggestions(self, prefix: str) -> Iterable[Suggestion]:
        if not prefix.startswith("@") or prefix.startswith(
            ("@selection", "@diagnostics")
        ):
            return ()
        query = prefix[1:].casefold()
        paths = self._load()
        ranked = sorted(
            (path for path in paths if query in path.casefold()),
            key=lambda path: (
                not path.casefold().startswith(query),
                len(path),
                path.casefold(),
            ),
        )
        return tuple(
            Suggestion(f"@{path}", "workspace file", "file")
            for path in ranked[: self.limit]
        )

    def _load(self) -> tuple[str, ...]:
        signature = self._workspace_signature()
        if self._paths and signature == self._signature:
            return self._paths
        try:
            completed = subprocess.run(
                ["git", "ls-files", "-co", "--exclude-standard", "-z"],
                cwd=self.workspace,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if completed.returncode != 0:
                raise OSError("git file listing failed")
            values = completed.stdout.decode("utf-8", errors="replace").split("\x00")
        except (OSError, subprocess.SubprocessError):
            values = [
                str(path.relative_to(self.workspace))
                for path in self.workspace.rglob("*")
                if path.is_file()
            ]
        self._paths = tuple(
            sorted(
                {
                    value
                    for value in values
                    if value
                    and not any(
                        part in {".git", ".capslock"} for part in Path(value).parts
                    )
                    and Path(value).name != ".env"
                    and not Path(value).name.startswith(".env.")
                }
            )
        )
        self._signature = signature
        return self._paths

    def _workspace_signature(self) -> tuple[int, int]:
        index = self.workspace / ".git" / "index"
        try:
            stat = index.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            stat = self.workspace.stat()
            return stat.st_mtime_ns, stat.st_size


class UnifiedSuggestionProvider:
    def __init__(self, providers: Iterable[SuggestionProvider]) -> None:
        self.providers = tuple(providers)

    def suggestions(self, prefix: str) -> tuple[Suggestion, ...]:
        values: dict[tuple[str, str], Suggestion] = {}
        for provider in self.providers:
            for item in provider.suggestions(prefix):
                values[(item.kind, item.value)] = item
        return tuple(values.values())


__all__ = [
    "StaticSuggestionProvider",
    "Suggestion",
    "SuggestionProvider",
    "UnifiedSuggestionProvider",
    "WorkspaceFileSuggestionProvider",
]
