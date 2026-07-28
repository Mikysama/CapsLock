"""Durable user-input request journal component."""

from __future__ import annotations

import json
from typing import Any

from ..core import now


def _validate_input_answers(questions: object, answers: object) -> None:
    if not isinstance(questions, list) or not isinstance(answers, dict):
        raise ValueError("answers must be an object keyed by question id")
    expected = {
        str(question.get("id")): question
        for question in questions
        if isinstance(question, dict) and isinstance(question.get("id"), str)
    }
    if set(answers) != set(expected):
        raise ValueError("answers must contain exactly one entry per question")
    for identifier, answer in answers.items():
        question = expected[identifier]
        multiple = bool(question.get("multiple", False))
        values = answer if isinstance(answer, list) else [answer]
        if multiple != isinstance(answer, list) or not values:
            raise ValueError(f"invalid answer shape for question {identifier}")
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError(f"answers for question {identifier} must be text")
        options = {
            str(option.get("value", option.get("label")))
            for option in question.get("options", [])
            if isinstance(option, dict)
        }
        allow_free = bool(question.get("allow_free_text", True))
        if not allow_free and any(value not in options for value in values):
            raise ValueError(
                f"answer is not an allowed option for question {identifier}"
            )


class InputRequestJournalRepository:
    async def create_input_request(
        self,
        *,
        request_id: str,
        session_id: str,
        run_id: str,
        invocation_id: str,
        questions: object,
        resume_data: dict[str, object],
    ) -> None:
        await self.execute(
            """INSERT INTO tool_input_requests(
                 id,session_id,run_id,invocation_id,status,questions_json,
                 resume_data_json,created_at
               ) VALUES(?,?,?,?, 'pending',?,?,?)""",
            (
                request_id,
                session_id,
                run_id,
                invocation_id,
                json.dumps(questions, ensure_ascii=False),
                json.dumps(resume_data, ensure_ascii=False),
                now(),
            ),
        )

    async def list_input_requests(
        self, session_id: str, *, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM tool_input_requests WHERE session_id=?"
        values: list[object] = [session_id]
        if status is not None:
            query += " AND status=?"
            values.append(status)
        query += " ORDER BY created_at,id"
        return [
            {
                "id": str(row["id"]),
                "session_id": str(row["session_id"]),
                "run_id": str(row["run_id"]),
                "invocation_id": str(row["invocation_id"]),
                "status": str(row["status"]),
                "questions": json.loads(row["questions_json"]),
                "answers": json.loads(row["answers_json"])
                if row["answers_json"] is not None
                else None,
                "created_at": str(row["created_at"]),
            }
            for row in await self.all(query, tuple(values))
        ]

    async def answer_input_request(
        self, request_id: str, session_id: str, answers: object
    ) -> dict[str, Any]:
        row = await self.one(
            "SELECT * FROM tool_input_requests WHERE id=? AND session_id=?",
            (request_id, session_id),
        )
        if row is None:
            raise ValueError("input request does not exist in this session")
        if row["status"] != "pending":
            raise ValueError("input request has already been resolved")
        questions = json.loads(row["questions_json"])
        _validate_input_answers(questions, answers)
        await self.execute(
            """UPDATE tool_input_requests SET status='answered',answers_json=?,answered_at=?
               WHERE id=? AND session_id=? AND status='pending'""",
            (json.dumps(answers, ensure_ascii=False), now(), request_id, session_id),
        )
        return {
            "id": request_id,
            "status": "answered",
            "answers": answers,
            "run_id": str(row["run_id"]),
            "invocation_id": str(row["invocation_id"]),
        }

    async def cancel_input_request(
        self, request_id: str, session_id: str
    ) -> dict[str, Any]:
        updated = await self.execute(
            """UPDATE tool_input_requests SET status='cancelled',answered_at=?
               WHERE id=? AND session_id=? AND status='pending'""",
            (now(), request_id, session_id),
        )
        if not updated:
            raise ValueError("pending input request does not exist in this session")
        return {"id": request_id, "status": "cancelled"}
