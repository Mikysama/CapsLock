"""Success-data contracts for the core builtin tools.

These schemas describe historical handler/service return values, not ToolPause
payloads or rendered tool content. Extensible action requests, metadata, user
answers, and agent-authored payloads deliberately remain open dictionaries.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_NUMBER = {"type": "number"}
_BOOLEAN = {"type": "boolean"}
_NULL_STRING = {"type": ["string", "null"]}
_NULL_INTEGER = {"type": ["integer", "null"]}
_MAPPING = {"type": "object"}


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _record(
    properties: dict[str, Any], *, optional: tuple[str, ...] = ()
) -> dict[str, Any]:
    # Additional fields are forward-compatible; declared fields remain typed.
    return {
        "type": "object",
        "properties": properties,
        "required": [name for name in properties if name not in optional],
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _union(*schemas: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": list(schemas)}


_STRINGS = _array(_STRING)
_TASK = _record(
    {
        "task_id": _STRING,
        "subject": _STRING,
        "description": _STRING,
        "owner": _NULL_STRING,
        "active_form": _NULL_STRING,
        "metadata": _MAPPING,
        "status": _STRING,
        "position": _INTEGER,
        "blocked_by": _STRINGS,
    }
)
_MEMORY = _record(
    {
        "memory_id": _STRING,
        "content": _NULL_STRING,
        "type": _STRING,
        "scope": _STRING,
        "source": _record({"kind": _STRING, "ref": _NULL_STRING}),
        "confidence": _NUMBER,
        "expires_at": _NULL_STRING,
        "revision": _INTEGER,
        "citation": _STRING,
    }
)
_SOURCE_FIELDS = {
    "source_id": _STRING,
    "url": _STRING,
    "title": _STRING,
    "excerpt": _STRING,
    "suspicious": _BOOLEAN,
}
_TEAM = _record(
    {
        "id": _STRING,
        "session_id": _STRING,
        "name": _STRING,
        "state": _STRING,
        "created_by_run_id": _NULL_STRING,
        "created_at": _STRING,
        "stopped_at": _NULL_STRING,
    }
)
_WORKER = _record(
    {
        "id": _STRING,
        "team_id": _STRING,
        "name": _STRING,
        "profile_json": _STRING,
        "workspace_mode": _STRING,
        "state": _STRING,
        "persistent": _INTEGER,
        "child_session_id": _NULL_STRING,
        "created_at": _STRING,
        "updated_at": _STRING,
        "stopped_at": _NULL_STRING,
    }
)
_AGENT_TASK = _record(
    {
        "id": _STRING,
        "parent_run_id": _STRING,
        "owner_session_id": _STRING,
        "team_id": _STRING,
        "assigned_worker_id": _NULL_STRING,
        "plan_task_id": _NULL_STRING,
        "objective": _STRING,
        "contract_json": _STRING,
        "contract_sha256": _STRING,
        "priority": _INTEGER,
        "state": _STRING,
        "child_run_id": _NULL_STRING,
        "child_workspace": _NULL_STRING,
        "claim_token": _NULL_STRING,
        "claim_expires_at": _NULL_STRING,
        "attempt_count": _INTEGER,
        "error": _NULL_STRING,
        "created_at": _STRING,
        "started_at": _NULL_STRING,
        "finished_at": _NULL_STRING,
    }
)
_ATTEMPT = _record(
    {
        "id": _STRING,
        "task_id": _STRING,
        "worker_id": _NULL_STRING,
        "ordinal": _INTEGER,
        "state": _STRING,
        "claim_token": _STRING,
        "child_run_id": _NULL_STRING,
        "reservation_json": _STRING,
        "usage_json": _STRING,
        "error": _NULL_STRING,
        "created_at": _STRING,
        "started_at": _NULL_STRING,
        "finished_at": _NULL_STRING,
    }
)
_APPROVAL = _record(
    {
        "id": _STRING,
        "attempt_id": _STRING,
        "child_action_id": _STRING,
        "parent_action_id": _NULL_STRING,
        "action_sha256": _STRING,
        "contract_sha256": _STRING,
        "state": _STRING,
        "payload_json": _STRING,
        "created_at": _STRING,
        "decided_at": _NULL_STRING,
    }
)
_BUDGET = _record(
    {
        "id": _STRING,
        "team_id": _STRING,
        "task_id": _STRING,
        "attempt_id": _STRING,
        "operation": _STRING,
        "amount_json": _STRING,
        "idempotency_key": _STRING,
        "created_at": _STRING,
    }
)
_MESSAGE = _record(
    {
        "id": _STRING,
        "task_id": _NULL_STRING,
        "parent_run_id": _STRING,
        "team_id": _NULL_STRING,
        "worker_id": _NULL_STRING,
        "attempt_id": _NULL_STRING,
        "sender": _STRING,
        "recipient": _STRING,
        "message_kind": _STRING,
        "payload": _MAPPING,
        "payload_sha256": _STRING,
        "status": _STRING,
        "created_at": _STRING,
        "expires_at": _NULL_STRING,
        "delivered_at": _NULL_STRING,
        "acknowledged_at": _NULL_STRING,
        "sender_address": _NULL_STRING,
        "recipient_address": _NULL_STRING,
        "reply_to_message_id": _NULL_STRING,
        "receiver_active": _BOOLEAN,
    },
    optional=(
        "sender_address",
        "recipient_address",
        "reply_to_message_id",
        "receiver_active",
    ),
)
_TEAM_MESSAGE = _record(
    {
        "team_id": _STRING,
        "broadcast": _BOOLEAN,
        "delivered": _array(_MESSAGE),
        "recipient_count": _INTEGER,
    }
)
_QUARANTINED_SUMMARY = _record(
    {
        "quarantined": _BOOLEAN,
        "source": _STRING,
        "bytes": _INTEGER,
        "sha256": _STRING,
        "risk_signals": _STRINGS,
        "content_available": _BOOLEAN,
        "error_code": _STRING,
        "artifact_id": _STRING,
        "read_with": _STRING,
    },
    optional=("content_available", "error_code", "artifact_id", "read_with"),
)
_AGENT_OUTPUT_FIELDS = {
    "task_id": _STRING,
    "state": _STRING,
    "summary": _STRING,
    "evidence": _array(_record({"path": _STRING, "sha256": _STRING})),
    "artifacts": _array(
        _record({"path": _STRING, "sha256": _STRING, "bytes": _INTEGER})
    ),
    "checks": _array(_record({"name": _STRING, "status": _STRING})),
    "usage": {"type": "object", "additionalProperties": _NUMBER},
    "verified": _BOOLEAN,
    "content_trust": _STRING,
    "verification_scope": _MAPPING,
    "error": _NULL_STRING,
    "memory_proposals": _array(
        _record(
            {
                "content": _STRING,
                "type": _STRING,
                "confidence": _NUMBER,
                "evidence_ids": _STRINGS,
                "applies_to_parent": _nullable(_BOOLEAN),
                "subject": _NULL_STRING,
                "why": _NULL_STRING,
                "how_to_apply": _NULL_STRING,
                "risk_flags": _STRINGS,
            },
            optional=(
                "applies_to_parent",
                "subject",
                "why",
                "how_to_apply",
                "risk_flags",
            ),
        )
    ),
}
_AGENT_OUTPUT = _record(_AGENT_OUTPUT_FIELDS)
_AGENT_STATUS = _record(
    {
        "wake_reason": _STRING,
        "pending_message_count": _INTEGER,
        "task_id": _STRING,
        "state": _STRING,
        "error": _NULL_STRING,
        "child_run_id": _NULL_STRING,
        "output": _nullable(_AGENT_OUTPUT),
    },
    optional=("wake_reason", "pending_message_count"),
)
_PLAN_DECISION = _record(
    {
        "choice": _STRING,
        "plan_id": _NULL_STRING,
        "feedback": _NULL_STRING,
    }
)


def _action(result_properties: dict[str, Any]) -> dict[str, Any]:
    # Actions keep their durable envelope, including nullable historical results.
    # Result dictionaries may also be produced by registered action handlers.
    return _record(
        {
            "action_id": _STRING,
            "kind": _STRING,
            "summary": _STRING,
            "status": _STRING,
            "result_kind": _NULL_STRING,
            "request": _MAPPING,
            "result": _nullable(
                _record(result_properties, optional=tuple(result_properties))
            ),
            "error": _NULL_STRING,
        }
    )


_FILE_ACTION = _action(
    {
        "path": _STRING,
        "operation": _STRING,
        "lsp_notification": _STRING,
        "lsp_error": _STRING,
    }
)
_WORKTREE_ACTION = _action({"active_workspace": _STRING, "operation": _STRING})

_SCHEMAS = {
    "ack_agent_message": _record({"acknowledged": _BOOLEAN}),
    "ask_user": _record({"answers": _MAPPING}),
    "assign_agent_task": _AGENT_STATUS,
    "create_agent_task": _AGENT_TASK,
    "create_agent_team": _TEAM,
    "create_file": _FILE_ACTION,
    "create_task": _TASK,
    "create_worktree": _WORKTREE_ACTION,
    "delegate_agents": _record(
        {
            "tasks": _array(
                _record(
                    {
                        **_AGENT_OUTPUT_FIELDS,
                        "summary": _union(_STRING, _QUARANTINED_SUMMARY),
                    }
                )
            ),
            "background": _BOOLEAN,
            "wake_reason": _STRING,
        },
        optional=("wake_reason",),
    ),
    "edit_file": _FILE_ACTION,
    "edit_notebook": _FILE_ACTION,
    "enter_plan_mode": _union(
        _record({"active": _BOOLEAN, "plan_id": _STRING, "already_active": _BOOLEAN}),
        _PLAN_DECISION,
    ),
    "exit_worktree": _WORKTREE_ACTION,
    "follow_up_agent": _AGENT_STATUS,
    "get_agent_task": _AGENT_STATUS,
    "get_agent_team": _record(
        {
            "team": _TEAM,
            "workers": _array(_WORKER),
            "tasks": _array(_AGENT_TASK),
            "attempts": _array(_ATTEMPT),
            "approvals": _array(_APPROVAL),
            "budget": _array(_BUDGET),
        }
    ),
    "get_memory": _MEMORY,
    "get_plan": _record(
        {
            "plan_id": _STRING,
            "objective": _STRING,
            "status": _STRING,
            "revision": _INTEGER,
            "sha256": _STRING,
            "content": _STRING,
        }
    ),
    "get_task": _TASK,
    "git_diff": _record({"output": _STRING}),
    "git_status": _record({"output": _STRING}),
    "glob_files": _record(
        {
            "pattern": _STRING,
            "path": _STRING,
            "files": _STRINGS,
            "count": _INTEGER,
            "truncated": _BOOLEAN,
            "backend": _STRING,
            "stop_reason": _NULL_STRING,
        }
    ),
    "list_external_sources": _array(
        _record(
            {
                **_SOURCE_FIELDS,
                "fetched_at": _STRING,
                "untrusted": _BOOLEAN,
            }
        )
    ),
    "list_files": _record(
        {
            "path": _STRING,
            "entries": _array(_record({"path": _STRING, "type": _STRING})),
            "files": _STRINGS,
            "count": _INTEGER,
            "offset": _INTEGER,
            "next_offset": _NULL_INTEGER,
            "truncated": _BOOLEAN,
            "stop_reason": _NULL_STRING,
        }
    ),
    "list_tasks": _record({"tasks": _array(_TASK)}),
    "load_skill": _record(
        {
            "name": _STRING,
            "description": _STRING,
            "scope": _STRING,
            "digest": _STRING,
            "instructions": _STRING,
            "resources": _array(
                _record(
                    {
                        "path": _STRING,
                        "size": _INTEGER,
                        "kind": _STRING,
                    }
                )
            ),
        }
    ),
    "process_output": _record(
        {
            "process_id": _STRING,
            "status": _STRING,
            "exit_code": _NULL_INTEGER,
            "stdout": _STRING,
            "stderr": _STRING,
            "stdout_offset": _INTEGER,
            "stderr_offset": _INTEGER,
            "progress_bytes": _INTEGER,
            "truncated": _BOOLEAN,
        }
    ),
    "process_stop": _record(
        {"process_id": _STRING, "status": _STRING, "exit_code": _NULL_INTEGER}
    ),
    "publish_agent_artifact": _record({"published": _BOOLEAN, "path": _STRING}),
    "read_agent_messages": _record({"messages": _array(_MESSAGE)}),
    "read_parent_messages": _record({"messages": _array(_MESSAGE)}),
    "send_parent_message": _MESSAGE,
    "ack_parent_message": _record({"acknowledged": _BOOLEAN}),
    "read_file": _record(
        {
            "path": _STRING,
            "sha256": _STRING,
            "start_line": _INTEGER,
            "end_line": _INTEGER,
            "evidence_id": _STRING,
            "total_lines": _INTEGER,
            "text": _STRING,
        }
    ),
    "read_image": _record(
        {
            "path": _STRING,
            "media_type": _STRING,
            "size_bytes": _INTEGER,
            "sha256": _STRING,
        }
    ),
    "read_notebook": _record(
        {
            "path": _STRING,
            "sha256": _STRING,
            "total_cells": _INTEGER,
            "offset": _INTEGER,
            "cells": _array(
                _record(
                    {
                        "index": _INTEGER,
                        "id": _NULL_STRING,
                        "cell_type": _NULL_STRING,
                        "source": _STRING,
                        "metadata": _MAPPING,
                        "outputs": _STRING,
                        "outputs_truncated": _BOOLEAN,
                    },
                    optional=("outputs", "outputs_truncated"),
                )
            ),
            "truncated": _BOOLEAN,
        }
    ),
    "read_pdf": _record(
        {
            "path": _STRING,
            "page_count": _INTEGER,
            "pages": _array(_record({"page": _INTEGER, "text": _STRING})),
            "rendered_pages": _INTEGER,
            "sha256": _STRING,
        }
    ),
    "read_skill_resource": _record(
        {
            "skill": _STRING,
            "path": _STRING,
            "kind": _STRING,
            "start_line": _INTEGER,
            "end_line": _INTEGER,
            "text": _STRING,
        }
    ),
    "read_tool_artifact": _record(
        {
            "warning": _STRING,
            "artifact_id": _STRING,
            "offset": _INTEGER,
            "bytes": _INTEGER,
            "content": _STRING,
            "next_offset": _NULL_INTEGER,
            "has_more": _BOOLEAN,
            "sha256": _STRING,
        }
    ),
    "resume_agent": _AGENT_STATUS,
    "search_files": _array(
        _record(
            {
                "id": _STRING,
                "path": _STRING,
                "start_line": _INTEGER,
                "end_line": _INTEGER,
                "text": _STRING,
            }
        )
    ),
    "search_memories": _array(_MEMORY),
    "search_session_history": _record(
        {
            "warning": _STRING,
            "query": _STRING,
            "count": _INTEGER,
            "hits": _array(
                _record(
                    {
                        "document_id": _INTEGER,
                        "run_id": _NULL_STRING,
                        "source_kind": _STRING,
                        "source_id": _STRING,
                        "chunk_ordinal": _INTEGER,
                        "content": _STRING,
                        "summary": _STRING,
                        "artifact_id": _NULL_STRING,
                        "score": _NUMBER,
                        "read_with": _STRING,
                    },
                    optional=("read_with",),
                )
            ),
        }
    ),
    "search_tools": _record(
        {"tools": _STRINGS, "count": _INTEGER, "available_next_turn": _BOOLEAN}
    ),
    "send_agent_message": _union(_MESSAGE, _TEAM_MESSAGE),
    "send_team_message": _TEAM_MESSAGE,
    "shell": _action(
        {
            "exit_code": _NULL_INTEGER,
            "stdout": _STRING,
            "stderr": _STRING,
            "truncated": _BOOLEAN,
            "timed_out": _BOOLEAN,
            "process_id": _STRING,
            "status": _STRING,
        }
    ),
    "start_agent": _WORKER,
    "stop_agent": _union(_WORKER, _AGENT_STATUS),
    "stop_agent_task": _AGENT_STATUS,
    "submit_plan": _PLAN_DECISION,
    "update_plan": _record(
        {"plan_id": _STRING, "revision": _INTEGER, "sha256": _STRING}
    ),
    "update_task": _TASK,
    "web_fetch": _action(
        {**_SOURCE_FIELDS, "truncated": _BOOLEAN, "untrusted": _BOOLEAN}
    ),
    "web_search": _action(
        {
            "query": _STRING,
            "results": _array(_record({"rank": _INTEGER, **_SOURCE_FIELDS})),
        }
    ),
    "write_file": _FILE_ACTION,
}


def builtin_output_schema(name: str) -> dict[str, Any]:
    """Return an isolated schema, failing closed for an unregistered builtin."""
    try:
        schema = _SCHEMAS[name]
    except KeyError:
        raise ValueError(f"missing output schema for builtin tool: {name}") from None
    return deepcopy(schema)


__all__ = ["builtin_output_schema"]
