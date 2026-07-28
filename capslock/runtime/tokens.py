"""Model-profile token estimation with conservative adaptive calibration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def heuristic_tokens(value: object) -> int:
    encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    return max(1, (len(encoded) + 2) // 3 + 8)


@dataclass(frozen=True)
class TokenBreakdown:
    system: int = 0
    history: int = 0
    attachments: int = 0
    memory: int = 0
    tools: int = 0
    total: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "system": self.system,
            "history": self.history,
            "attachments": self.attachments,
            "memory": self.memory,
            "tools": self.tools,
            "total": self.total,
        }


class AdaptiveTokenEstimator:
    """Calibrate a provider-neutral heuristic against per-request API usage."""

    def __init__(
        self,
        profile: str,
        *,
        settings_store: Any = None,
        safety_margin: float = 1.15,
        strategy: str = "adaptive",
    ) -> None:
        self.profile = profile
        self.settings_store = settings_store
        self.safety_margin = safety_margin
        self.strategy = strategy
        self.ratio = 1.0
        self.samples = 0
        self._loaded = False

    @property
    def key(self) -> str:
        return f"token_calibration:{self.profile}"

    async def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.strategy != "adaptive":
            return
        if self.settings_store is None or not hasattr(self.settings_store, "workspace"):
            return
        try:
            raw = await self.settings_store.workspace(self.key)
            value = json.loads(raw) if raw else {}
            ratio = float(value.get("ratio", 1.0))
            samples = int(value.get("samples", 0))
            if 0.25 <= ratio <= 4.0 and samples >= 0:
                self.ratio, self.samples = ratio, samples
        except (TypeError, ValueError, json.JSONDecodeError):
            return

    def estimate(self, value: object) -> int:
        if self.strategy.startswith("tiktoken:"):
            try:
                import tiktoken

                encoding = tiktoken.get_encoding(self.strategy.split(":", 1)[1])
                source = json.dumps(value, ensure_ascii=False, default=str)
                return max(1, len(encoding.encode(source)) + 8)
            except (ImportError, KeyError, ValueError):
                pass
        base = heuristic_tokens(value)
        if self.strategy == "heuristic":
            return base
        # The first calls deliberately reserve extra room. Once calibrated, keep a
        # smaller floor so one anomalous provider report cannot erase all headroom.
        margin = (
            self.safety_margin
            if self.samples < 3
            else max(1.05, self.safety_margin - 0.05)
        )
        return max(1, int(base * self.ratio * margin + 0.999))

    async def observe(self, value: object, actual_input_tokens: int) -> None:
        if actual_input_tokens <= 0 or self.strategy != "adaptive":
            return
        await self.load()
        base = heuristic_tokens(value)
        observed = min(4.0, max(0.25, actual_input_tokens / max(1, base)))
        weight = 1.0 if self.samples == 0 else 0.2
        self.ratio = self.ratio * (1.0 - weight) + observed * weight
        self.samples += 1
        if self.settings_store is None or not hasattr(
            self.settings_store, "set_workspace"
        ):
            return
        payload = json.dumps(
            {"ratio": round(self.ratio, 6), "samples": self.samples},
            sort_keys=True,
            separators=(",", ":"),
        )
        await self.settings_store.set_workspace(self.key, payload)


__all__ = ["AdaptiveTokenEstimator", "TokenBreakdown", "heuristic_tokens"]
