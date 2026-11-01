"""Bounded source-mailbox reads and receiver-owned context checkpoints.

New message bodies live in the workspace agent_mailbox table. A mailbox run
step records IDs already added to model context. Pending pre-v23 receipts are
temporarily held in run steps until the receiver commits their delivery.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .core import Repository, now


def bounded_messages(
    messages: list[dict[str, Any]], *, limit: int, max_bytes: int
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    total = 0
    for message in messages:
        size = len(
            json.dumps(
                message.get("payload", {}),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        )
        if len(selected) >= limit or total + size > max_bytes:
            break
        selected.append(message)
        total += size
    return {"messages": selected, "has_more": len(selected) < len(messages)}


class MailboxCheckpointRepository(Repository):
    async def delivered_ids(self, session_id: str) -> set[str]:
        """Find durable delivery evidence across all runs of this session."""
        rows = await self.all(
            """SELECT s.mailbox_ids_json FROM run_steps s JOIN runs r ON r.id=s.run_id
               WHERE r.session_id=? AND s.kind='mailbox' AND s.status='completed'
                 AND s.mailbox_ids_json IS NOT NULL ORDER BY s.started_at,s.id""",
            (session_id,),
        )
        delivered: set[str] = set()
        for row in rows:
            delivered.update(str(value) for value in json.loads(str(row["mailbox_ids_json"])))
        return delivered

    async def historical_pending(self, session_id: str) -> list[dict[str, Any]]:
        """Drain pre-v23 receipts moved into run steps during migration."""
        rows = await self.all(
            """SELECT s.mailbox_pending_json,s.mailbox_pending_sha256 FROM run_steps s
               JOIN runs r ON r.id=s.run_id
               WHERE r.session_id=? AND s.mailbox_pending_json IS NOT NULL
               ORDER BY s.started_at,s.id LIMIT 33""",
            (session_id,),
        )
        messages = []
        for row in rows[:32]:
            encoded = str(row["mailbox_pending_json"])
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["mailbox_pending_sha256"]:
                raise ValueError("pending mailbox receipt digest does not match")
            messages.append(json.loads(encoded))
        return messages

    async def clear_historical(self, session_id: str, message_ids: list[str]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        await self.execute(
            f"""DELETE FROM run_steps WHERE mailbox_pending_json IS NOT NULL
                AND json_extract(mailbox_pending_json,'$.id') IN ({placeholders})
                AND run_id IN (SELECT id FROM runs WHERE session_id=?)""",
            (*message_ids, session_id),
        )

    async def commit_delivery(
        self,
        session_id: str,
        run_id: str,
        message_ids: list[str],
        checkpoint: dict[str, Any],
    ) -> str:
        ids = list(dict.fromkeys(message_ids))
        if not ids:
            raise ValueError("mailbox delivery has no messages")
        step_id, timestamp = uuid.uuid4().hex, now()
        async with self.database.transaction() as connection:
            run = await (
                await connection.execute(
                    "SELECT session_id FROM runs WHERE id=?", (run_id,)
                )
            ).fetchone()
            if run is None or run["session_id"] != session_id:
                raise ValueError("mailbox run does not belong to recipient session")
            ordinal = await (
                await connection.execute(
                    "SELECT coalesce(max(ordinal),-1)+1 FROM run_steps WHERE run_id=?",
                    (run_id,),
                )
            ).fetchone()
            await connection.execute(
                """INSERT INTO run_steps(id,run_id,ordinal,kind,status,checkpoint_json,started_at,finished_at,mailbox_ids_json)
                   VALUES(?,?,?,'mailbox','completed',?,?,?,?)""",
                (
                    step_id,
                    run_id,
                    int(ordinal[0]),
                    json.dumps(checkpoint, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    json.dumps(ids),
                ),
            )
            await connection.execute(
                """UPDATE run_steps AS previous SET checkpoint_json=NULL
                   WHERE run_id=? AND id<>?
                     AND checkpoint_json IS NOT NULL
                     AND status NOT IN ('waiting_approval','waiting_input')
                     AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.resume_from_step_id=previous.id)""",
                (run_id, step_id),
            )
        return step_id


# Keep the import name used by existing bootstrap integrations.
MailboxDeliveryRepository = MailboxCheckpointRepository
