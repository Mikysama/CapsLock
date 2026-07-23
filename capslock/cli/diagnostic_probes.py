"""Focused probe types registered by the doctor composition layer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .diagnostic_core import Diagnostic


ProbeFunction = Callable[[Any], Awaitable[list[Diagnostic]]]


class _FocusedProbe:
    def __init__(self, inspect: ProbeFunction) -> None:
        self.inspect = inspect

    async def probe(self, context: Any) -> list[Diagnostic]:
        return await self.inspect(context)


class ConfigurationProbe(_FocusedProbe):
    pass


class DatabaseProbe(_FocusedProbe):
    pass


class McpProbe(_FocusedProbe):
    pass


class SkillProbe(_FocusedProbe):
    pass


class PluginProbe(_FocusedProbe):
    pass


class ProviderProbe(_FocusedProbe):
    pass
