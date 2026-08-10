"""Composite repository for run, tool, permission, and input journals."""

from __future__ import annotations

import json

from ..core import Repository, now
from .events import RunEventJournalRepository
from .input_requests import InputRequestJournalRepository
from .permissions import PermissionJournalRepository
from .tool_invocations import ToolInvocationJournalRepository


class RunJournalRepository(
    PermissionJournalRepository,
    ToolInvocationJournalRepository,
    InputRequestJournalRepository,
    RunEventJournalRepository,
    Repository,
):
    def __init__(self, database, *, episodic=None) -> None:
        super().__init__(database)
        self.episodic = episodic

    async def interrupt_active(self) -> None:
        """Close crash-left journal records before accepting new workspace work."""
        timestamp = now()
        preview = json.dumps(
            {
                "status": "cancelled",
                "ok": False,
                "executed": False,
                "delivery_status": "inline",
                "data": {},
                "content": [],
                "error": "workspace process ended during tool execution",
                "error_code": "process_interrupted",
            }
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                """UPDATE tool_invocations
                   SET status='cancelled',execution_status='cancelled',delivery_status='inline',
                       result_preview=?,error_code='process_interrupted',finished_at=?,
                       duration_ms=coalesce(duration_ms,0)
                   WHERE status IN ('received','validating','authorizing','queued','running')""",
                (preview, timestamp),
            )
            await connection.execute(
                """UPDATE run_steps SET status='cancelled',finished_at=?,
                       error=coalesce(error,'workspace process interrupted')
                   WHERE status='running'""",
                (timestamp,),
            )
