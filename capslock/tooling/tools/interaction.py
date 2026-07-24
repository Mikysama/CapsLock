"""Interaction direct-capability tool execution."""

from __future__ import annotations

import asyncio  # noqa: F401
import base64  # noqa: F401
import fnmatch  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import shutil  # noqa: F401
import uuid  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

from ...domain import ActionRecord, ActionStatus, ActionType  # noqa: F401
from ...evidence import Evidence  # noqa: F401
from ...security import TEXT_SUFFIXES  # noqa: F401
from ..contracts import (  # noqa: F401
    ExecutionContext,
    ToolContent,
    ToolExecution,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
)
from .actions import execute_action_tool  # noqa: F401
from .support import _outcome, _path  # noqa: F401


async def ask_user(
    context: ExecutionContext, arguments: dict[str, Any]
) -> ToolExecution:
    del context
    questions = arguments.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        raise ValueError("ask_user requires between one and three questions")
    identifiers: set[str] = set()
    normalized: list[dict[str, object]] = []
    for index, raw in enumerate(questions):
        if not isinstance(raw, dict):
            raise ValueError("each question must be an object")
        identifier = str(raw.get("id") or f"question_{index + 1}")
        prompt = raw.get("question")
        options = raw.get("options")
        if (
            identifier in identifiers
            or not isinstance(prompt, str)
            or not prompt.strip()
        ):
            raise ValueError("question ids must be unique and prompts non-empty")
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise ValueError("each question requires between two and four options")
        normalized_options = []
        for option in options:
            if isinstance(option, str):
                normalized_options.append({"label": option, "value": option})
            elif isinstance(option, dict) and isinstance(option.get("label"), str):
                normalized_options.append(
                    {
                        "label": option["label"],
                        "value": str(option.get("value", option["label"])),
                        **(
                            {"description": option["description"]}
                            if isinstance(option.get("description"), str)
                            else {}
                        ),
                    }
                )
            else:
                raise ValueError("question options must be strings or labeled objects")
        identifiers.add(identifier)
        normalized.append(
            {
                "id": identifier,
                "question": prompt.strip(),
                "options": normalized_options,
                "multiple": bool(raw.get("multiple", False)),
                "allow_free_text": True,
            }
        )
    request_id = f"input_{uuid.uuid4().hex}"
    return ToolPause("user_input", request_id, {"questions": normalized}, {})


async def resume_ask_user(
    context: ExecutionContext,
    arguments: dict[str, Any],
    pause: ToolPause,
    response: object,
    reporter: Any,
) -> ToolExecution:
    del context, arguments, pause, reporter
    if response is None:
        return ToolOutcome(
            ToolOutcomeStatus.CANCELLED,
            False,
            error="user cancelled the input request",
            error_code="input_cancelled",
        )
    if not isinstance(response, dict):
        return ToolOutcome.failure(
            "input answers must be an object", code="invalid_input_answers"
        )
    return ToolOutcome.success({"answers": response})


def interaction_tools():
    from ..contracts import ResolvedToolPolicy, define_tool
    from .schemas import _schema, _str

    ResolvedToolPolicy.safe_read()
    return [
        define_tool(
            "ask_user",
            "Pause this invocation and ask the user one to three structured questions.",
            _schema(
                {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": _str(),
                                "question": _str(),
                                "options": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 4,
                                    "items": {
                                        "oneOf": [
                                            _str(),
                                            {
                                                "type": "object",
                                                "properties": {
                                                    "label": _str(),
                                                    "value": _str(),
                                                    "description": _str(),
                                                },
                                                "required": ["label"],
                                                "additionalProperties": False,
                                            },
                                        ]
                                    },
                                },
                                "multiple": {"type": "boolean"},
                            },
                            "required": ["question", "options"],
                            "additionalProperties": False,
                        },
                    }
                },
                ["questions"],
            ),
            ask_user,
            policy=ResolvedToolPolicy(context_mutation=True),
            resume=resume_ask_user,
        ),
    ]


__all__ = ["ask_user", "interaction_tools"]
