"""Composable diagnostic probe and repair registries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    subject: str
    message: str
    fixable: bool = False


class DiagnosticProbe(Protocol):
    async def probe(self, context: Any) -> list[Diagnostic]: ...


class DiagnosticProbeRegistry:
    def __init__(self) -> None:
        self._probes: list[DiagnosticProbe] = []

    def register(self, probe: DiagnosticProbe) -> None:
        self._probes.append(probe)

    async def run(self, context: Any) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for probe in self._probes:
            diagnostics.extend(await probe.probe(context))
        return diagnostics


DiagnosticFix = Callable[[Any], Awaitable[None]]


class DiagnosticFixRegistry:
    def __init__(self) -> None:
        self._fixes: dict[str, DiagnosticFix] = {}

    def register(self, code: str, fix: DiagnosticFix) -> None:
        self._fixes[code] = fix

    async def apply(self, diagnostic: Diagnostic, context: Any) -> None:
        fix = self._fixes.get(diagnostic.code)
        if fix is None:
            raise ValueError(f"no diagnostic fix is registered: {diagnostic.code}")
        await fix(context)
