"""Persistent run-governance snapshots and tool-attempt history."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from ...domain import BudgetSnapshot, RunLimits, RunMode, StopReason
from .core import Repository, now


class GovernanceRepository(Repository):
    async def start(
        self,
        run_id: str,
        *,
        parent_run_id: str | None,
        mode: RunMode,
        limits: RunLimits,
    ) -> tuple[BudgetSnapshot, list[dict[str, Any]]]:
        parent = await self._row(parent_run_id) if parent_run_id else None
        if parent is None:
            snapshot = BudgetSnapshot(mode, run_id, limits)
            history: list[dict[str, Any]] = []
        else:
            limits = _expanded_limits(_limits(parent["limits_json"]), limits)
            snapshot = BudgetSnapshot(
                mode,
                str(parent["root_run_id"]),
                limits,
                tool_rounds=int(parent["tool_rounds"]),
                tool_calls=int(parent["tool_calls"]),
                duration_ms=int(parent["elapsed_ms"]),
                input_tokens=int(parent["input_tokens"]),
                output_tokens=int(parent["output_tokens"]),
                cost_usd=float(parent["cost_usd"]),
                extensions=int(parent["extensions"]),
            )
            history = list(json.loads(parent["history_json"]))[-64:]
        await self.execute(
            """INSERT INTO run_governance(
                   run_id,root_run_id,mode,limits_json,tool_rounds,tool_calls,
                   elapsed_ms,input_tokens,output_tokens,cost_usd,extensions,
                   history_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                snapshot.root_run_id,
                mode.value,
                json.dumps(limits.as_dict(), separators=(",", ":")),
                snapshot.tool_rounds,
                snapshot.tool_calls,
                snapshot.duration_ms,
                snapshot.input_tokens,
                snapshot.output_tokens,
                snapshot.cost_usd,
                snapshot.extensions,
                json.dumps(history, separators=(",", ":")),
                now(),
            ),
        )
        return snapshot, history

    async def load(
        self, run_id: str
    ) -> tuple[BudgetSnapshot, list[dict[str, Any]]] | None:
        row = await self._row(run_id)
        if row is None:
            return None
        reason = StopReason(row["stop_reason"]) if row["stop_reason"] else None
        return (
            BudgetSnapshot(
                RunMode(row["mode"]),
                str(row["root_run_id"]),
                _limits(row["limits_json"]),
                int(row["tool_rounds"]),
                int(row["tool_calls"]),
                int(row["elapsed_ms"]),
                int(row["input_tokens"]),
                int(row["output_tokens"]),
                float(row["cost_usd"]),
                int(row["extensions"]),
                reason,
            ),
            list(json.loads(row["history_json"])),
        )

    async def latest_for_session(self, session_id: str) -> BudgetSnapshot | None:
        row = await self.one(
            """SELECT g.run_id FROM run_governance g
               JOIN runs r ON r.id=g.run_id WHERE r.session_id=?
               ORDER BY r.started_at DESC LIMIT 1""",
            (session_id,),
        )
        if row is None:
            return None
        loaded = await self.load(str(row["run_id"]))
        return loaded[0] if loaded else None

    async def save(
        self,
        run_id: str,
        snapshot: BudgetSnapshot,
        history: list[dict[str, Any]],
    ) -> None:
        await self.execute(
            """UPDATE run_governance SET limits_json=?,tool_rounds=?,tool_calls=?,
                   elapsed_ms=?,input_tokens=?,output_tokens=?,cost_usd=?,extensions=?,
                   history_json=?,stop_reason=?,updated_at=? WHERE run_id=?""",
            (
                json.dumps(snapshot.limits.as_dict(), separators=(",", ":")),
                snapshot.tool_rounds,
                snapshot.tool_calls,
                snapshot.duration_ms,
                snapshot.input_tokens,
                snapshot.output_tokens,
                snapshot.cost_usd,
                snapshot.extensions,
                json.dumps(history[-64:], separators=(",", ":")),
                snapshot.stop_reason.value if snapshot.stop_reason else None,
                now(),
                run_id,
            ),
        )

    async def reserve_attempt(
        self,
        run_id: str,
        *,
        round_index: int,
        name: str,
        arguments: dict[str, Any],
        fingerprint: str,
    ) -> int:
        row = await self.one(
            "SELECT coalesce(max(sequence),0)+1 FROM tool_call_attempts WHERE run_id=?",
            (run_id,),
        )
        sequence = int(row[0]) if row else 1
        await self.execute(
            """INSERT INTO tool_call_attempts(
                   run_id,sequence,round_index,name,arguments_json,fingerprint,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                run_id,
                sequence,
                round_index,
                name,
                json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                fingerprint,
                now(),
            ),
        )
        row = await self.one(
            "SELECT id FROM tool_call_attempts WHERE run_id=? AND sequence=?",
            (run_id, sequence),
        )
        assert row is not None
        return int(row[0])

    async def finish_attempt(
        self, attempt_id: int, *, ok: bool, duration_ms: int
    ) -> None:
        await self.execute(
            """UPDATE tool_call_attempts SET ok=?,duration_ms=?,finished_at=?
               WHERE id=? AND ok IS NULL""",
            (int(ok), max(0, duration_ms), now(), attempt_id),
        )

    async def _row(self, run_id: str | None):
        if run_id is None:
            return None
        return await self.one("SELECT * FROM run_governance WHERE run_id=?", (run_id,))


def _limits(value: str) -> RunLimits:
    return RunLimits(**json.loads(value))


def _expanded_limits(previous: RunLimits, requested: RunLimits) -> RunLimits:
    def expanded(old, new):
        if old is None:
            return new
        if new is None:
            return old
        return max(old, new)

    return replace(
        requested,
        max_tool_rounds=max(previous.max_tool_rounds, requested.max_tool_rounds),
        max_tool_calls=expanded(previous.max_tool_calls, requested.max_tool_calls),
        max_duration_seconds=expanded(
            previous.max_duration_seconds, requested.max_duration_seconds
        ),
        max_tokens=expanded(previous.max_tokens, requested.max_tokens),
        max_budget_usd=expanded(previous.max_budget_usd, requested.max_budget_usd),
    )
