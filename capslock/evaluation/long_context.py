"""Validation and task expansion for external anonymized long sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import EvaluationTask


def load_long_context_tasks(
    path: Path, *, split: str
) -> tuple[EvaluationTask, ...]:
    document = json.loads(path.read_text(encoding="utf-8"))
    sessions = document.get("sessions") if isinstance(document, dict) else None
    if not isinstance(sessions, list) or len(sessions) < 4:
        raise ValueError("long-context dataset requires at least 4 sessions")
    tasks: list[EvaluationTask] = []
    fact_count = continuation_count = 0
    participating_sessions: set[str] = set()
    for session in sessions:
        if not isinstance(session, dict):
            raise TypeError("long-context sessions must be objects")
        session_id = str(session.get("id", "")).strip()
        history = session.get("messages")
        if not session_id or not isinstance(history, list) or not history:
            raise ValueError("each long-context session requires id and messages")
        normalized_history = [_history_message(item) for item in history]
        for kind, field in (
            ("fact", "fact_questions"),
            ("continuation", "continuation_tasks"),
        ):
            cases = session.get(field, [])
            if not isinstance(cases, list):
                raise TypeError(f"long-context {field} must be an array")
            for index, case in enumerate(cases):
                if not isinstance(case, dict):
                    raise TypeError(f"long-context {field} entries must be objects")
                case_split = str(case.get("split", "confirm"))
                if case_split != split:
                    continue
                prompt = str(case.get("prompt", "")).strip()
                expected = str(case.get("expected", "")).strip()
                position = str(case.get("position", "middle"))
                if not prompt or not expected or position not in {
                    "front",
                    "middle",
                    "tail",
                }:
                    raise ValueError(
                        f"invalid long-context case {session_id}:{field}:{index}"
                    )
                tasks.append(
                    EvaluationTask(
                        f"{split}-long-context-{session_id}-{kind}-{index:03d}",
                        "context",
                        split,
                        prompt,
                        {
                            "position": position,
                            "kind": kind,
                            "history": normalized_history,
                            "expected_answer": expected,
                            "pressure": float(case.get("pressure", 0.9)),
                            "required_turns": int(case.get("required_turns", 6)),
                            "required_tokens": int(
                                case.get("required_tokens", 32_000)
                            ),
                            "compaction_failures": 0,
                            "in_budget": True,
                            "capacity_case": False,
                        },
                        critical=bool(case.get("critical", False)),
                    )
                )
                participating_sessions.add(session_id)
                if kind == "fact":
                    fact_count += 1
                else:
                    continuation_count += 1
    if len(participating_sessions) < 4:
        raise ValueError(
            "long-context dataset requires cases from at least 4 sessions "
            "for the selected split"
        )
    if fact_count < 60 or continuation_count < 30:
        raise ValueError(
            "long-context dataset requires at least 60 fact questions and "
            "30 continuation tasks for the selected split"
        )
    return tuple(tasks)


def _history_message(value: Any) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("long-context messages must be objects")
    role = str(value.get("role", ""))
    if role not in {"user", "assistant", "tool"}:
        raise ValueError("long-context message role must be user, assistant, or tool")
    message = {str(key): item for key, item in value.items() if key != "id"}
    message["role"] = role
    message.setdefault("content", "")
    return message


__all__ = ["load_long_context_tasks"]
