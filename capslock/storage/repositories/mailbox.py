"""Receiver-owned durable mailbox receipts and atomic context checkpoints."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .core import Repository, now

ACTIONABLE_KINDS = frozenset({"instruction", "question", "response", "cancel"})


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


class MailboxDeliveryRepository(Repository):
    async def accept(
        self, session_id: str, run_id: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not messages:
            return []
        async with self.database.transaction() as connection:
            run = await (
                await connection.execute(
                    "SELECT session_id FROM runs WHERE id=?", (run_id,)
                )
            ).fetchone()
            if run is None or run["session_id"] != session_id:
                raise ValueError("mailbox run does not belong to recipient session")
            for message in messages:
                encoded = json.dumps(
                    message, ensure_ascii=False, sort_keys=True, default=str
                )
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                existing = await (
                    await connection.execute(
                        "SELECT envelope_json FROM mailbox_deliveries WHERE recipient_session_id=? AND message_id=?",
                        (session_id, message["id"]),
                    )
                ).fetchone()
                if existing is not None:
                    previous = json.loads(existing["envelope_json"])
                    # Source delivery timestamps/status can change on retry; identity and body cannot.
                    for key in (
                        "payload_sha256",
                        "sender_address",
                        "recipient_address",
                        "task_id",
                        "message_kind",
                        "reply_to_message_id",
                    ):
                        if previous.get(key) != message.get(key):
                            raise ValueError("mailbox receipt identity does not match")
                    continue
                await connection.execute(
                    """INSERT INTO mailbox_deliveries(recipient_session_id,run_id,message_id,task_id,message_kind,envelope_json,envelope_sha256,received_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        session_id,
                        run_id,
                        message["id"],
                        message.get("task_id"),
                        message["message_kind"],
                        encoded,
                        digest,
                        now(),
                    ),
                )
        return (await self.pending(session_id, run_id))["messages"]

    async def pending(
        self,
        session_id: str,
        run_id: str,
        *,
        limit: int = 32,
        max_bytes: int = 65_536,
        actionable_only: bool = False,
    ) -> dict[str, Any]:
        limit, max_bytes = (
            min(max(int(limit), 1), 32),
            min(max(int(max_bytes), 0), 65_536),
        )
        groups = ["message_kind IN ('instruction','question','response','cancel')"]
        if not actionable_only:
            groups.append("message_kind IN ('progress','artifact_offer')")
        batches = []
        for group in groups:
            batches.append(
                await self.all(
                    f"SELECT * FROM mailbox_deliveries WHERE recipient_session_id=? AND run_id=? AND status='received' AND {group} ORDER BY receipt_sequence LIMIT ?",
                    (session_id, run_id, limit + 1),
                )
            )
        active = batches[0]
        passive = [] if actionable_only else batches[1]
        priority = min(24, limit) if passive else limit
        rows = active[:priority] + passive + active[priority:]
        messages = []
        for row in rows:
            encoded = str(row["envelope_json"])
            if (
                hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                != row["envelope_sha256"]
            ):
                raise ValueError("mailbox receipt digest does not match")
            message = json.loads(encoded)
            message["receipt_sequence"] = int(row["receipt_sequence"])
            messages.append(message)
        return bounded_messages(messages, limit=limit, max_bytes=max_bytes)

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
        placeholders = ",".join("?" for _ in ids)
        step_id, timestamp = uuid.uuid4().hex, now()
        async with self.database.transaction() as connection:
            rows = await (
                await connection.execute(
                    f"SELECT message_id FROM mailbox_deliveries WHERE recipient_session_id=? AND run_id=? AND status='received' AND message_id IN ({placeholders})",
                    (session_id, run_id, *ids),
                )
            ).fetchall()
            if len(rows) != len(ids):
                raise ValueError("mailbox messages are not pending in this run")
            ordinal = await (
                await connection.execute(
                    "SELECT coalesce(max(ordinal),-1)+1 FROM run_steps WHERE run_id=?",
                    (run_id,),
                )
            ).fetchone()
            await connection.execute(
                """INSERT INTO run_steps(id,run_id,ordinal,kind,status,checkpoint_json,started_at,finished_at) VALUES(?,?,?,'mailbox','completed',?,?,?)""",
                (
                    step_id,
                    run_id,
                    int(ordinal[0]),
                    json.dumps(checkpoint, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            await connection.execute(
                f"UPDATE mailbox_deliveries SET status='delivered',delivered_at=? WHERE recipient_session_id=? AND run_id=? AND message_id IN ({placeholders})",
                (timestamp, session_id, run_id, *ids),
            )
            await connection.execute(
                """UPDATE run_steps AS previous SET checkpoint_json=NULL WHERE run_id=? AND id<>? AND checkpoint_json IS NOT NULL
                AND status NOT IN ('waiting_approval','waiting_input') AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.resume_from_step_id=previous.id)""",
                (run_id, step_id),
            )
        return step_id

    async def rebind_pending(
        self,
        session_id: str,
        from_run_ids: list[str],
        run_id: str,
        *,
        task_ids: list[str] | None = None,
        activate_historical: bool = False,
    ) -> int:
        if not from_run_ids:
            return 0
        target = await self.one("SELECT session_id FROM runs WHERE id=?", (run_id,))
        if target is None or target["session_id"] != session_id:
            raise ValueError("mailbox run does not belong to recipient session")
        clause = ",".join("?" for _ in from_run_ids)
        values: list[Any] = [run_id, session_id, *from_run_ids]
        scope = ""
        if task_ids is not None:
            scope = (
                " AND (task_id IS NULL OR task_id IN ("
                + ",".join("?" for _ in task_ids)
                + "))"
            )
            values.extend(task_ids)
        states = "('received','historical')" if activate_historical else "('received')"
        return await self.execute(
            f"UPDATE mailbox_deliveries SET run_id=?,status='received' WHERE recipient_session_id=? AND run_id IN ({clause}) AND status IN {states}{scope}",
            tuple(values),
        )

    async def rebind_task_pending(
        self,
        session_id: str,
        task_id: str,
        run_id: str,
        *,
        activate_historical: bool = False,
    ) -> int:
        rows = await self.all(
            "SELECT DISTINCT run_id FROM mailbox_deliveries WHERE recipient_session_id=? AND task_id=?",
            (session_id, task_id),
        )
        return await self.rebind_pending(
            session_id,
            [str(row["run_id"]) for row in rows],
            run_id,
            task_ids=[task_id],
            activate_historical=activate_historical,
        )
