"""Deterministic metadata catalog with deferred dynamic discovery."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .contracts import ToolContract, ToolDefinition


@dataclass(frozen=True)
class ToolCatalogSnapshot:
    generation: int
    fingerprint: str
    tools: tuple[ToolDefinition, ...]
    schemas: tuple[dict[str, object], ...]


class ToolCatalog:
    """Atomic, deterministic tool catalog with provider-neutral deferred loading."""

    def __init__(
        self,
        tools: Iterable[ToolDefinition],
        *,
        schema_budget_tokens: int = 8_000,
    ) -> None:
        self.schema_budget_tokens = max(1, schema_budget_tokens)
        self._generation = 1
        self._discovered: set[str] = set()
        self._dynamic_provider: Any = None
        self._dynamic_names: set[str] = set()
        self._replace(tools, increment=False)

    def _replace(
        self, tools: Iterable[ToolDefinition], *, increment: bool = True
    ) -> None:
        values: dict[str, ToolDefinition] = {}
        for tool in tools:
            if tool.name in values:
                raise ValueError(f"duplicate tool name: {tool.name}")
            values[tool.name] = tool
        ordered = dict(sorted(values.items()))
        changed = not hasattr(self, "_tools") or [
            item.contract.metadata() for item in ordered.values()
        ] != [item.contract.metadata() for item in self._tools.values()]
        self._tools = ordered
        if increment and changed:
            self._generation += 1

    def refresh(self, tools: Iterable[ToolDefinition]) -> ToolCatalogSnapshot:
        self._replace(tools)
        self._discovered.intersection_update(self._tools)
        return self.snapshot()

    def configure_dynamic(
        self,
        provider: Callable[[], Any],
        initial: Iterable[ToolDefinition] = (),
    ) -> None:
        dynamic = tuple(initial)
        self._dynamic_provider = provider
        self._dynamic_names = {item.name for item in dynamic}
        self._replace([*self._tools.values(), *dynamic])

    async def refresh_dynamic(self) -> ToolCatalogSnapshot:
        if self._dynamic_provider is None:
            return self.snapshot()
        current = [
            tool
            for name, tool in self._tools.items()
            if name not in self._dynamic_names
        ]
        dynamic = self._dynamic_provider()
        if inspect.isawaitable(dynamic):
            dynamic = await dynamic
        values = tuple(dynamic)
        self._dynamic_names = {item.name for item in values}
        self._replace([*current, *values])
        self._discovered.intersection_update(self._tools)
        return self.snapshot()

    def combined(self, tools: Iterable[ToolDefinition]) -> "ToolCatalog":
        return ToolCatalog(
            [*self._tools.values(), *tools],
            schema_budget_tokens=self.schema_budget_tokens,
        )

    def filtered(self, names: set[str]) -> "ToolCatalog":
        return ToolCatalog(
            [tool for name, tool in self._tools.items() if name in names],
            schema_budget_tokens=self.schema_budget_tokens,
        )

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def contract(self, name: str) -> ToolContract | None:
        tool = self.get(name)
        return None if tool is None else tool.contract

    def discover(self, names: Iterable[str]) -> tuple[str, ...]:
        selected = tuple(sorted(name for name in names if name in self._tools))
        self._discovered.update(selected)
        return selected

    def search(self, query: str, limit: int = 5) -> tuple[str, ...]:
        terms = tuple(item.casefold() for item in query.split() if item.strip())
        scored: list[tuple[int, str]] = []
        for name, tool in self._tools.items():
            if not tool.contract.deferred:
                continue
            haystack = " ".join(
                filter(
                    None,
                    (name, tool.contract.search_hint, tool.contract.description),
                )
            ).casefold()
            score = sum(
                4 if term in name.casefold() else 1
                for term in terms
                if term in haystack
            )
            if score:
                scored.append((score, name))
        selected = tuple(
            name
            for _, name in sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]
        )
        return self.discover(selected)

    def snapshot(self) -> ToolCatalogSnapshot:
        selected: list[ToolDefinition] = []
        used_tokens = 0
        for tool in self._tools.values():
            if tool.contract.deferred and tool.name not in self._discovered:
                continue
            schema = tool.schema()
            estimated = max(1, len(json.dumps(schema, sort_keys=True)) // 4)
            if (
                tool.contract.deferred
                and used_tokens + estimated > self.schema_budget_tokens
            ):
                continue
            selected.append(tool)
            used_tokens += estimated
        schemas = tuple(tool.schema() for tool in selected)
        encoded = json.dumps(schemas, sort_keys=True, separators=(",", ":"))
        return ToolCatalogSnapshot(
            self._generation,
            hashlib.sha256(encoded.encode()).hexdigest(),
            tuple(selected),
            schemas,
        )

    @property
    def schemas(self) -> list[dict[str, object]]:
        return list(self.snapshot().schemas)


__all__ = ["ToolCatalog", "ToolCatalogSnapshot"]
