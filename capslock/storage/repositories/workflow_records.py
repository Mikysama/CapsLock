"""Row mappers shared by workflow repositories."""

from __future__ import annotations

import json

from ...domain import (
    AgentEvent,
    AgentEventKind,
    RunInfo,
    RunStepInfo,
    RunStepKind,
    RunStepStatus,
    WorkItemInfo,
    WorkItemStatus,
)


def work_item(row) -> WorkItemInfo:
    return WorkItemInfo(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        question=str(row["question"]),
        status=WorkItemStatus(row["status"]),
        position=int(row["position"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        current_run_id=row["current_run_id"],
        parent_work_item_id=row["parent_work_item_id"],
        error=row["error"],
    )


def run(row) -> RunInfo:
    return RunInfo(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        work_item_id=str(row["work_item_id"]),
        question=str(row["question"]),
        status=str(row["status"]),
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        cost_usd=float(row["cost_usd"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        parent_run_id=row["parent_run_id"],
        resume_from_step_id=row["resume_from_step_id"],
        stop_reason=row["stop_reason"],
    )


def step(row) -> RunStepInfo:
    return RunStepInfo(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        ordinal=int(row["ordinal"]),
        kind=RunStepKind(row["kind"]),
        status=RunStepStatus(row["status"]),
        checkpoint=json.loads(row["checkpoint_json"])
        if row["checkpoint_json"]
        else None,
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
        error=row["error"],
    )


def event(row) -> AgentEvent:
    return AgentEvent(
        int(row["sequence"]),
        str(row["created_at"]),
        str(row["session_id"]),
        str(row["run_id"]),
        str(row["work_item_id"]),
        AgentEventKind(row["event_kind"]),
        json.loads(row["payload_json"]),
    )
