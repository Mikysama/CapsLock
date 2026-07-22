"""Declarative portable-import table and reference specifications."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImportTableSpec:
    name: str
    primary_key: str | tuple[str, ...]


WORKSPACE_IMPORT_SPECS = tuple(
    ImportTableSpec(name, key)
    for name, key in (
        ("sessions", "id"),
        ("work_items", "id"),
        ("runs", "id"),
        ("run_steps", "id"),
        ("run_events", "id"),
        ("messages", "id"),
        ("actions", "id"),
        ("tasks", "id"),
        ("sources", "id"),
        ("tool_calls", "id"),
        ("citations", "id"),
        ("workspace_settings", "key"),
        ("skill_settings", "name"),
        ("routing_decisions", "id"),
        ("model_calls", "id"),
        ("budget_decisions", "id"),
        ("run_governance", "run_id"),
        ("tool_call_attempts", "id"),
        ("agent_tasks", "id"),
        ("agent_capabilities", ("task_id", "ordinal")),
        ("agent_messages", "id"),
        ("agent_outputs", "task_id"),
    )
)

MEMORY_IMPORT_SPECS = tuple(
    ImportTableSpec(name, key)
    for name, key in (
        ("memories", "id"),
        ("memory_revisions", ("memory_id", "revision")),
        ("memory_workspace_settings", "workspace_key"),
        ("memory_extractions", "id"),
        ("memory_candidates", "id"),
        ("memory_sources", "id"),
        ("memory_recalls", "run_id"),
        ("memory_recall_items", ("run_id", "memory_id")),
        (
            "memory_accesses",
            ("memory_id", "revision", "workspace_key", "session_id", "run_id"),
        ),
        ("memory_audit", "id"),
    )
)

WORKSPACE_TABLES = tuple(item.name for item in WORKSPACE_IMPORT_SPECS)
MEMORY_TABLES = tuple(item.name for item in MEMORY_IMPORT_SPECS)
WORKSPACE_PRIMARY = {item.name: item.primary_key for item in WORKSPACE_IMPORT_SPECS}
MEMORY_PRIMARY = {item.name: item.primary_key for item in MEMORY_IMPORT_SPECS}

REFERENCE_FIELDS = {
    "session_id": "sessions",
    "run_id": "runs",
    "work_item_id": "work_items",
    "parent_work_item_id": "work_items",
    "parent_run_id": "runs",
    "resume_from_step_id": "run_steps",
    "root_run_id": "runs",
    "routing_decision_id": "routing_decisions",
    "memory_id": "memories",
    "related_memory_id": "memories",
    "adopted_memory_id": "memories",
    "extraction_id": "memory_extractions",
    "source_run_id": "runs",
    "task_id": "agent_tasks",
}
