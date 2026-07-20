"""Model routing, call metering, and budget persistence."""

from __future__ import annotations

import json
import uuid
from typing import Any

from .core import Repository, now


class ModelRepository(Repository):
    async def record_decision(
        self,
        run_id: str,
        *,
        role: str,
        candidates: list[dict[str, Any]],
        selected: str | None,
        reasons: dict[str, Any],
    ) -> int:
        row = await self.one(
            "SELECT coalesce(max(sequence),0)+1 FROM routing_decisions WHERE run_id=?",
            (run_id,),
        )
        sequence = int(row[0]) if row else 1
        await self.execute(
            """INSERT INTO routing_decisions(run_id,sequence,role,candidates_json,selected_profile,reason_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                run_id,
                sequence,
                role,
                json.dumps(candidates, ensure_ascii=False),
                selected,
                json.dumps(reasons, ensure_ascii=False),
                now(),
            ),
        )
        row = await self.one(
            "SELECT id FROM routing_decisions WHERE run_id=? AND sequence=?",
            (run_id, sequence),
        )
        assert row is not None
        return int(row[0])

    async def start_call(
        self,
        run_id: str,
        *,
        decision_id: int,
        role: str,
        profile: str,
        provider: str,
        model: str,
        attempt: int,
        data_policy: str,
        fallback_from: str | None,
    ) -> str:
        identifier = uuid.uuid4().hex
        await self.execute(
            """INSERT INTO model_calls(id,run_id,routing_decision_id,role,profile,provider,model,attempt,status,data_policy,fallback_from,started_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                run_id,
                decision_id,
                role,
                profile,
                provider,
                model,
                attempt,
                "running",
                data_policy,
                fallback_from,
                now(),
            ),
        )
        return identifier

    async def finish_call(
        self,
        identifier: str,
        *,
        duration_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        status = "failed" if error_code else "completed"
        await self.execute(
            """UPDATE model_calls SET status=?,finished_at=?,duration_ms=?,input_tokens=?,output_tokens=?,cost_usd=?,error_code=?,error_message=?
               WHERE id=? AND status='running'""",
            (
                status,
                now(),
                max(0, duration_ms),
                max(0, input_tokens),
                max(0, output_tokens),
                max(0.0, cost_usd),
                error_code,
                error_message,
                identifier,
            ),
        )

    async def usage(self, run_id: str) -> tuple[int, int, float]:
        row = await self.one(
            """SELECT coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),coalesce(sum(cost_usd),0)
               FROM model_calls WHERE run_id=?""",
            (run_id,),
        )
        assert row is not None
        return int(row[0]), int(row[1]), float(row[2])

    async def session_cost(self, run_id: str) -> float:
        row = await self.one(
            """SELECT coalesce(sum(c.cost_usd),0) FROM model_calls c JOIN runs current ON current.id=?
               JOIN runs r ON r.id=c.run_id AND r.session_id=current.session_id""",
            (run_id,),
        )
        return float(row[0]) if row else 0.0

    async def summary(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self.all(
            """SELECT provider,model,role,count(*) calls,sum(input_tokens) input_tokens,
                      sum(output_tokens) output_tokens,sum(cost_usd) cost_usd,
                      sum(CASE WHEN status='failed' THEN 1 ELSE 0 END) errors,
                      sum(coalesce(duration_ms,0)) duration_ms
               FROM model_calls WHERE run_id=? GROUP BY provider,model,role ORDER BY role,provider,model""",
            (run_id,),
        )
        return [dict(row) for row in rows]

    async def record_budget(
        self,
        run_id: str,
        *,
        scope: str,
        limit_type: str,
        current: float,
        reserved: float,
        limit: float,
        decision: str,
        profile: str,
    ) -> None:
        await self.execute(
            """INSERT INTO budget_decisions(run_id,scope,limit_type,current_value,reserved_value,limit_value,decision,profile,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                scope,
                limit_type,
                current,
                reserved,
                limit,
                decision,
                profile,
                now(),
            ),
        )
