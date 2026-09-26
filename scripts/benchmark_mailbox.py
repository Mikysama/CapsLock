#!/usr/bin/env python3
"""Measure real mailbox batch queries and committed-message notification latency.

Uses a new temporary workspace database under --directory (ordinary disk by
default), never opens a user's workspace database, and does not call a model.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capslock.application.workflow import WorkflowService  # noqa: E402
from capslock.collaboration.models import (  # noqa: E402
    AgentTaskContract,
    MailboxMessageKind,
)
from capslock.collaboration.wakeup import MailboxWakeupRegistry  # noqa: E402
from capslock.storage.repositories import WorkspaceRepositories  # noqa: E402


def _distribution(samples: list[float]) -> dict[str, int | float]:
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "maximum_ms": ordered[-1],
    }


async def benchmark(
    directory: Path,
    *,
    history_rows: int = 1_000_000,
    pending_rows: int = 10_000,
    iterations: int = 100,
) -> dict[str, object]:
    if history_rows < 0 or pending_rows < 0 or iterations < 1:
        raise ValueError("row counts must be nonnegative and iterations positive")
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError("benchmark directory must already exist")
    with tempfile.TemporaryDirectory(prefix="capslock-mailbox-", dir=directory) as root:
        workspace = Path(root)
        repositories = await WorkspaceRepositories.open(
            workspace / "benchmark.sqlite3", workspace=workspace
        )
        try:
            return await _measure(
                repositories, directory, history_rows, pending_rows, iterations
            )
        finally:
            await repositories.close()


async def _measure(
    repositories: WorkspaceRepositories,
    directory: Path,
    history_rows: int,
    pending_rows: int,
    iterations: int,
) -> dict[str, object]:
    session = await repositories.sessions.create("offline-benchmark")
    workflow = WorkflowService(
        repositories.work_items,
        repositories.runs,
        repositories.run_journal,
        repositories.workflow,
    )
    prepared = await workflow.prepare(session.id, "Measure mailbox storage")
    contract = AgentTaskContract.create(prepared.run.id, "Mailbox benchmark")
    await repositories.collaboration.create_task(contract)
    address = f"session:{session.id}"
    payload = json.dumps({"text": "x" * 1000}, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    timestamp = datetime.now(UTC).isoformat()
    insert = """INSERT INTO agent_mailbox(
        id,task_id,parent_run_id,sender,recipient,message_kind,payload_json,
        payload_sha256,status,created_at,sender_address,recipient_address)
        VALUES(?,?,?,'child','parent',?,?,?,?,?,?,?)"""
    seed_start = time.perf_counter()
    for start in range(0, history_rows + pending_rows, 5000):
        rows = []
        for index in range(start, min(start + 5000, history_rows + pending_rows)):
            acknowledged = index < history_rows
            rows.append(
                (
                    f"mail_benchmark_{index:012d}",
                    contract.task_id,
                    prepared.run.id,
                    "question" if index % 4 else "progress",
                    payload,
                    digest,
                    "acknowledged" if acknowledged else "queued",
                    timestamp,
                    f"task:{contract.task_id}",
                    address,
                )
            )
        async with repositories.database.transaction() as connection:
            await connection.executemany(insert, rows)
    seed_seconds = time.perf_counter() - seed_start
    plans: list[str] = []
    for group in (
        "message_kind IN ('instruction','question','response','cancel')",
        "message_kind IN ('progress','artifact_offer')",
    ):
        rows = await repositories.database.fetch_all(
            "EXPLAIN QUERY PLAN SELECT * FROM agent_mailbox "
            "WHERE recipient_address=? AND status IN ('queued','delivered') "
            "AND (expires_at IS NULL OR expires_at>?) AND "
            + group
            + " ORDER BY created_at,id LIMIT ?",
            (address, timestamp, 33),
        )
        plans.extend(str(row[3]) for row in rows)
    samples: list[float] = []
    tracemalloc.start()
    changes_before = repositories.database.connection.total_changes
    try:
        for _ in range(iterations):
            start = time.perf_counter()
            batch = await repositories.collaboration.read_mailbox_batch(address)
            samples.append((time.perf_counter() - start) * 1000)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    writes = repositories.database.connection.total_changes - changes_before
    registry = MailboxWakeupRegistry()
    await registry.register(address)
    notifications: list[float] = []
    try:
        for index in range(iterations):
            snapshot = await registry.snapshot(address)
            assert snapshot is not None

            async def wait_for_notification(previous=snapshot) -> float:
                result = await registry.wait_since(
                    address, previous, timeout=30, actionable_only=True
                )
                if result is None or result.closed:
                    raise RuntimeError("mailbox notification failed")
                return time.perf_counter()

            waiting = asyncio.create_task(wait_for_notification())
            try:
                await asyncio.sleep(0)
                await repositories.collaboration.send_mailbox(
                    task_id=contract.task_id,
                    parent_run_id=prepared.run.id,
                    sender="child",
                    recipient="parent",
                    kind=MailboxMessageKind.QUESTION,
                    payload={"text": f"notification {index}"},
                )
                committed = time.perf_counter()
                await registry.notify(address)
                returned = await waiting
                notifications.append((returned - committed) * 1000)
            finally:
                if not waiting.done():
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
    finally:
        await registry.unregister(address)
    messages = batch["messages"]
    body_bytes = sum(
        len(json.dumps(message["payload"], ensure_ascii=False, sort_keys=True).encode())
        for message in messages
    )
    notification = {**_distribution(notifications), "target_p95_ms": 100}
    notification["target_met"] = notification["p95_ms"] < 100
    return {
        "benchmark": "capslock-mailbox-v1",
        "measured_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "directory": str(directory),
        "storage": "temporary SQLite file under selected directory; WAL enabled",
        "history_rows": history_rows,
        "pending_rows": pending_rows,
        "seed_seconds": seed_seconds,
        "query_plan": plans,
        "batch": {
            "count": len(messages),
            "payload_bytes": body_bytes,
            "has_more": batch["has_more"],
            "actionable_count": sum(
                message["message_kind"]
                in {"instruction", "question", "response", "cancel"}
                for message in messages
            ),
            "passive_count": sum(
                message["message_kind"] in {"progress", "artifact_offer"}
                for message in messages
            ),
        },
        "reads": {
            **_distribution(samples),
            "writes": writes,
            "peak_python_bytes": peak_bytes,
        },
        "notification": notification,
        "limitations": [
            "Synthetic offline storage workload; no model calls or task-success claims.",
            "Read timings include tracemalloc overhead; repeated reads may use the OS cache.",
            "Notification starts after send_mailbox returns committed data; excludes SQL commit latency.",
            "Notification measures an idle waiter, not message delivery to a model.",
            "Filesystem characteristics are controlled by --directory, not inferred by this script.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path.cwd())
    parser.add_argument("--history-rows", type=int, default=1_000_000)
    parser.add_argument("--pending-rows", type=int, default=10_000)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    try:
        report = asyncio.run(
            benchmark(
                args.directory,
                history_rows=args.history_rows,
                pending_rows=args.pending_rows,
                iterations=args.iterations,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
