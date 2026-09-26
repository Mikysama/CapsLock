"""Multi-dimensional run governance and deterministic loop detection."""

from __future__ import annotations

import asyncio
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
from ..ports import GovernancePort, ModelAuditPort


class RunGovernor:
    def __init__(
        self,
        governance: GovernancePort,
        models: ModelAuditPort,
        run_id: str,
        snapshot: BudgetSnapshot,
        history: list[dict[str, Any]],
        loop_settings: LoopDetectionSettings,
    ) -> None:
        self.governance = governance
        self.models = models
        self.run_id = run_id
        self.snapshot = snapshot
        accounting = next(
            (
                item["_collaboration_accounted"]
                for item in history
                if "_collaboration_accounted" in item
            ),
            {},
        )
        self.history = [
            item for item in history if "_collaboration_accounted" not in item
        ]
        self.loop_settings = loop_settings
        self.started = time.monotonic()
        self.base_duration_ms = snapshot.duration_ms
        self.base_input_tokens = snapshot.input_tokens
        self.base_output_tokens = snapshot.output_tokens
        self.base_cost_usd = snapshot.cost_usd
        self.observed_input_tokens = 0
        self.observed_output_tokens = 0
        self._tool_attempt_lock = asyncio.Lock()
        self._collaboration_budget: Any = None
        self._child_usage: dict[str, float] = (
            accounting if accounting.get("run_id") == run_id else {}
        )
        if self._child_usage:
            self.base_input_tokens -= int(self._child_usage.get("input_tokens", 0))
            self.base_output_tokens -= int(self._child_usage.get("output_tokens", 0))
            self.base_cost_usd -= float(self._child_usage.get("cost_usd", 0))
        self._child_reserved: dict[str, float] = {}

    @classmethod
    async def create(
        cls,
        governance: GovernancePort,
        models: ModelAuditPort,
        run_id: str,
        *,
        parent_run_id: str | None,
        mode: RunMode,
        limits: RunLimits,
        loop_settings: LoopDetectionSettings,
    ) -> "RunGovernor":
        snapshot, history = await governance.start(
            run_id, parent_run_id=parent_run_id, mode=mode, limits=limits
        )
        return cls(governance, models, run_id, snapshot, history, loop_settings)

    def attach_collaboration_budget(self, repository: Any) -> None:
        self._collaboration_budget = repository

    def available_remaining(self) -> dict[str, Any]:
        remaining = dict(self.snapshot.as_dict()["remaining"])
        for key, value in self._child_reserved.items():
            if remaining.get(key) is not None:
                remaining[key] = max(0, remaining[key] - value)
        return remaining

    async def current(self) -> BudgetSnapshot:
        input_tokens, output_tokens, cost = await self.models.usage(self.run_id)
        usage: dict[str, float] = {}
        if self._collaboration_budget is not None:
            budget = await self._collaboration_budget.parent_budget(self.run_id)
            usage = budget["settled"]
            self._child_reserved = budget["reserved"]
        self.snapshot = replace(
            self.snapshot,
            duration_ms=self.base_duration_ms
            + round((time.monotonic() - self.started) * 1000),
            input_tokens=self.base_input_tokens
            + max(input_tokens, self.observed_input_tokens)
            + int(usage.get("input_tokens", 0)),
            output_tokens=self.base_output_tokens
            + max(output_tokens, self.observed_output_tokens)
            + int(usage.get("output_tokens", 0)),
            cost_usd=self.base_cost_usd + cost + usage.get("cost_usd", 0),
            tool_rounds=self.snapshot.tool_rounds
            + int(
                usage.get("tool_rounds", 0) - self._child_usage.get("tool_rounds", 0)
            ),
            tool_calls=self.snapshot.tool_calls
            + int(usage.get("tool_calls", 0) - self._child_usage.get("tool_calls", 0)),
        )
        self._child_usage = {**usage, "run_id": self.run_id} if usage else {}
        await self._save()
        return self.snapshot

    async def record_model_usage(
        self, input_tokens: int, output_tokens: int
    ) -> BudgetSnapshot:
        self.observed_input_tokens += max(0, input_tokens)
        self.observed_output_tokens += max(0, output_tokens)
        return await self.current()

    async def record_external_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0,
        tool_rounds: int = 0,
        tool_calls: int = 0,
    ) -> BudgetSnapshot:
        self.observed_input_tokens += max(0, input_tokens)
        self.observed_output_tokens += max(0, output_tokens)
        self.base_cost_usd += max(0.0, cost_usd)
        self.snapshot = replace(
            await self.current(),
            tool_rounds=self.snapshot.tool_rounds + max(0, tool_rounds),
            tool_calls=self.snapshot.tool_calls + max(0, tool_calls),
        )
        await self._save()
        return self.snapshot

    async def before_model(self) -> None:
        await self._check_common()
        if self.available_remaining()["tool_rounds"] <= 0:
            await self.stop(StopReason.MAX_TOOL_ROUNDS)

    async def record_round(self) -> BudgetSnapshot:
        self.snapshot = replace(
            await self.current(), tool_rounds=self.snapshot.tool_rounds + 1
        )
        await self._save()
        return self.snapshot

    async def before_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        trusted_poll: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        async with self._tool_attempt_lock:
            await self._check_common()
            limit = self.snapshot.limits.max_tool_calls
            if limit is not None and self.available_remaining()["tool_calls"] <= 0:
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
            if (
                trusted_poll is not None
                and trusted_poll.get("status") == "running"
                and (detail is None or detail.get("pattern") != "failed_retry")
            ):
                stalled = time.monotonic() - float(trusted_poll["last_progress_at"])
                detail = (
                    {"pattern": "process_stalled", "seconds": stalled}
                    if stalled >= 120
                    else None
                )
            if detail is not None:
                await self.stop(StopReason.REPEATED_TOOL_CALL, detail=detail)
            attempt_id = await self.governance.reserve_attempt(
                self.run_id,
                round_index=max(1, self.snapshot.tool_rounds),
                name=normalized_name,
                arguments=safe_arguments,
                fingerprint=fingerprint,
            )
            self.history.append(
                {
                    "attempt_id": attempt_id,
                    "fingerprint": fingerprint,
                    "ok": None,
                    "running_poll": trusted_poll is not None
                    and trusted_poll.get("status") == "running",
                }
            )
            self.snapshot = replace(
                self.snapshot, tool_calls=self.snapshot.tool_calls + 1
            )
            await self._save()
            return attempt_id, safe_arguments, fingerprint

    async def finish_tool(self, attempt_id: int, *, ok: bool, duration_ms: int) -> None:
        async with self._tool_attempt_lock:
            await self.governance.finish_attempt(
                attempt_id, ok=ok, duration_ms=duration_ms
            )
            record = next(
                (
                    item
                    for item in reversed(self.history)
                    if item.get("attempt_id") == attempt_id
                ),
                None,
            )
            if record is not None:
                record["ok"] = ok
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
        if limits.max_tokens is not None and self.available_remaining()["tokens"] <= 0:
            await self.stop(StopReason.MAX_TOKENS)
        if (
            limits.max_budget_usd is not None
            and self.available_remaining()["budget_usd"] <= 0
        ):
            await self.stop(StopReason.MAX_BUDGET_USD)

    def remaining_seconds(self) -> float | None:
        limit = self.snapshot.limits.max_duration_seconds
        if limit is None:
            return None
        elapsed = self.base_duration_ms / 1000 + (time.monotonic() - self.started)
        return max(0.0, limit - elapsed)

    def _loop_detail(self, fingerprint: str) -> dict[str, Any] | None:
        fingerprints = [
            f"running_poll:{item.get('attempt_id')}"
            if item.get("running_poll") and item.get("ok") is True
            else str(item.get("fingerprint"))
            for item in self.history
        ]
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
        history = self.history
        if self._child_usage:
            history = [*history[-63:], {"_collaboration_accounted": self._child_usage}]
        await self.governance.save(self.run_id, self.snapshot, history)
