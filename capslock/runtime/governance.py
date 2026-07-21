"""Multi-dimensional run governance and deterministic loop detection."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from typing import Any

from ..domain import (
    BudgetSnapshot,
    LoopDetectionSettings,
    RunLimits,
    RunMode,
    RunStopped,
    StopReason,
)
from ..security import redact
from ..storage.repositories_v2 import WorkspaceRepositories


class RunGovernor:
    def __init__(
        self,
        repositories: WorkspaceRepositories,
        run_id: str,
        snapshot: BudgetSnapshot,
        history: list[dict[str, Any]],
        loop_settings: LoopDetectionSettings,
    ) -> None:
        self.repositories = repositories
        self.run_id = run_id
        self.snapshot = snapshot
        self.history = history
        self.loop_settings = loop_settings
        self.started = time.monotonic()
        self.base_duration_ms = snapshot.duration_ms
        self.base_input_tokens = snapshot.input_tokens
        self.base_output_tokens = snapshot.output_tokens
        self.base_cost_usd = snapshot.cost_usd
        self.observed_input_tokens = 0
        self.observed_output_tokens = 0

    @classmethod
    async def create(
        cls,
        repositories: WorkspaceRepositories,
        run_id: str,
        *,
        parent_run_id: str | None,
        mode: RunMode,
        limits: RunLimits,
        loop_settings: LoopDetectionSettings,
    ) -> "RunGovernor":
        snapshot, history = await repositories.governance.start(
            run_id, parent_run_id=parent_run_id, mode=mode, limits=limits
        )
        return cls(repositories, run_id, snapshot, history, loop_settings)

    async def current(self) -> BudgetSnapshot:
        input_tokens, output_tokens, cost = await self.repositories.models.usage(
            self.run_id
        )
        self.snapshot = replace(
            self.snapshot,
            duration_ms=self.base_duration_ms
            + round((time.monotonic() - self.started) * 1000),
            input_tokens=self.base_input_tokens
            + max(input_tokens, self.observed_input_tokens),
            output_tokens=self.base_output_tokens
            + max(output_tokens, self.observed_output_tokens),
            cost_usd=self.base_cost_usd + cost,
        )
        await self._save()
        return self.snapshot

    async def record_model_usage(
        self, input_tokens: int, output_tokens: int
    ) -> BudgetSnapshot:
        self.observed_input_tokens += max(0, input_tokens)
        self.observed_output_tokens += max(0, output_tokens)
        return await self.current()

    async def before_model(self) -> None:
        await self._check_common()
        if self.snapshot.tool_rounds >= self.snapshot.limits.max_tool_rounds:
            await self.stop(StopReason.MAX_TOOL_ROUNDS)

    async def record_round(self) -> BudgetSnapshot:
        self.snapshot = replace(
            await self.current(), tool_rounds=self.snapshot.tool_rounds + 1
        )
        await self._save()
        return self.snapshot

    async def before_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[int, dict[str, Any], str]:
        await self._check_common()
        limit = self.snapshot.limits.max_tool_calls
        if limit is not None and self.snapshot.tool_calls >= limit:
            await self.stop(StopReason.MAX_TOOL_CALLS)
        safe_arguments = redact(arguments)
        assert isinstance(safe_arguments, dict)
        normalized_name = name.strip().casefold()
        payload = json.dumps(
            safe_arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fingerprint = hashlib.sha256(
            f"{normalized_name}\n{payload}".encode("utf-8")
        ).hexdigest()
        detail = self._loop_detail(fingerprint)
        if detail is not None:
            await self.stop(StopReason.REPEATED_TOOL_CALL, detail=detail)
        attempt_id = await self.repositories.governance.reserve_attempt(
            self.run_id,
            round_index=max(1, self.snapshot.tool_rounds),
            name=normalized_name,
            arguments=safe_arguments,
            fingerprint=fingerprint,
        )
        self.history.append({"fingerprint": fingerprint, "ok": None})
        self.snapshot = replace(self.snapshot, tool_calls=self.snapshot.tool_calls + 1)
        await self._save()
        return attempt_id, safe_arguments, fingerprint

    async def finish_tool(self, attempt_id: int, *, ok: bool, duration_ms: int) -> None:
        await self.repositories.governance.finish_attempt(
            attempt_id, ok=ok, duration_ms=duration_ms
        )
        if self.history:
            self.history[-1]["ok"] = ok
        await self.current()

    async def extend_tool_rounds(self, increment: int = 32) -> BudgetSnapshot:
        self.snapshot = replace(
            await self.current(),
            limits=replace(
                self.snapshot.limits,
                max_tool_rounds=self.snapshot.limits.max_tool_rounds + increment,
            ),
            extensions=self.snapshot.extensions + 1,
            stop_reason=None,
        )
        await self._save()
        return self.snapshot

    async def stop(
        self, reason: StopReason, *, detail: dict[str, Any] | None = None
    ) -> None:
        snapshot = replace(await self.current(), stop_reason=reason)
        self.snapshot = snapshot
        await self._save()
        raise RunStopped(
            reason,
            snapshot,
            detail=detail,
            summarize=self.snapshot.mode is RunMode.INTERACTIVE
            and reason is StopReason.MAX_TOOL_ROUNDS,
        )

    async def _check_common(self) -> None:
        snapshot = await self.current()
        limits = snapshot.limits
        if limits.max_duration_seconds is not None and snapshot.duration_ms >= round(
            limits.max_duration_seconds * 1000
        ):
            await self.stop(StopReason.MAX_DURATION)
        if limits.max_tokens is not None and snapshot.tokens >= limits.max_tokens:
            await self.stop(StopReason.MAX_TOKENS)
        if (
            limits.max_budget_usd is not None
            and snapshot.cost_usd >= limits.max_budget_usd
        ):
            await self.stop(StopReason.MAX_BUDGET_USD)

    def remaining_seconds(self) -> float | None:
        limit = self.snapshot.limits.max_duration_seconds
        if limit is None:
            return None
        elapsed = self.base_duration_ms / 1000 + (time.monotonic() - self.started)
        return max(0.0, limit - elapsed)

    def _loop_detail(self, fingerprint: str) -> dict[str, Any] | None:
        fingerprints = [str(item.get("fingerprint")) for item in self.history]
        prospective = fingerprints + [fingerprint]
        failed = 0
        for item in reversed(self.history):
            if item.get("fingerprint") != fingerprint or item.get("ok") is not False:
                break
            failed += 1
        if failed + 1 >= self.loop_settings.failed_retries:
            return {
                "pattern": "failed_retry",
                "repetitions": failed + 1,
                "fingerprint": fingerprint,
            }
        repeated = self.loop_settings.consecutive_repeats
        if len(prospective) >= repeated and len(set(prospective[-repeated:])) == 1:
            return {
                "pattern": "consecutive",
                "repetitions": repeated,
                "fingerprint": fingerprint,
            }
        repetitions = self.loop_settings.cycle_repetitions
        for period in range(2, self.loop_settings.max_cycle_length + 1):
            width = period * repetitions
            if len(prospective) < width:
                continue
            candidate = prospective[-period:]
            if prospective[-width:] == candidate * repetitions:
                return {
                    "pattern": "cycle",
                    "cycle_length": period,
                    "repetitions": repetitions,
                    "fingerprint": fingerprint,
                }
        return None

    async def _save(self) -> None:
        await self.repositories.governance.save(
            self.run_id, self.snapshot, self.history
        )
