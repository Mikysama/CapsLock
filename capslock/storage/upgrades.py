"""Transactional, backup-first workspace schema upgrades."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import aiosqlite


async def upgrade_workspace_schema(
    path: Path,
    connection: aiosqlite.Connection,
    *,
    source_version: int | None = None,
) -> Path:
    if source_version is None:
        row = await (await connection.execute("PRAGMA user_version")).fetchone()
        source_version = int(row[0])
    if source_version not in {6, 7}:
        raise ValueError(f"unsupported workspace upgrade source: {source_version}")
    checkpoint = await connection.execute("PRAGMA wal_checkpoint(FULL)")
    await checkpoint.close()
    await connection.commit()
    backup = path.parent / "backups" / (
        f"capslock-v{source_version}-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        + ".sqlite3"
    )
    await asyncio.to_thread(_backup, path, backup)
    try:
        if source_version == 6:
            await connection.executescript(_UPGRADE_FIRST_STEP)
        await connection.executescript(_UPGRADE_SECOND_STEP)
    except BaseException:
        await connection.rollback()
        raise
    return backup


def _backup(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    target_path.chmod(0o600)


_UPGRADE_FIRST_STEP = """
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN IMMEDIATE;
ALTER TABLE run_events RENAME TO run_events_v6;
CREATE TABLE run_events (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  event_id TEXT NOT NULL UNIQUE,
  trace_id TEXT NOT NULL,
  event_kind TEXT NOT NULL CHECK(event_kind IN ('queued','thinking','text_delta','tool_queued','tool_running','tool_progress','tool_permission','tool_completed','tool_cancelled','budget_updated','limit_reached','budget_extended','waiting_approval','completed','failed','cancelled','stopped')),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,sequence)
) STRICT;
INSERT INTO run_events SELECT * FROM run_events_v6;
DROP TABLE run_events_v6;
CREATE INDEX idx_run_events_run ON run_events(run_id,sequence);

ALTER TABLE tool_invocations RENAME TO tool_invocations_v6;
CREATE TABLE tool_invocations (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  tool_call_id TEXT NOT NULL,
  name TEXT NOT NULL,
  spec_json TEXT NOT NULL CHECK(json_valid(spec_json)),
  capabilities_json TEXT NOT NULL CHECK(json_valid(capabilities_json)),
  resolved_policy_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(resolved_policy_json)),
  arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
  status TEXT NOT NULL CHECK(status IN ('received','validating','authorizing','queued','running','completed','failed','cancelled')),
  execution_status TEXT CHECK(execution_status IS NULL OR execution_status IN ('succeeded','failed','denied','cancelled','pending_approval')),
  delivery_status TEXT CHECK(delivery_status IS NULL OR delivery_status IN ('inline','artifact','truncated','delivery_failed')),
  timings_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(timings_json)),
  result_preview TEXT,
  artifact_id TEXT,
  error_code TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
  UNIQUE(run_id,sequence)
) STRICT;
INSERT INTO tool_invocations(
  id,run_id,session_id,sequence,tool_call_id,name,spec_json,capabilities_json,
  arguments_json,status,execution_status,delivery_status,result_preview,artifact_id,
  error_code,started_at,finished_at,duration_ms
)
SELECT id,run_id,session_id,sequence,tool_call_id,name,spec_json,capabilities_json,
       arguments_json,
       CASE WHEN status='running' THEN 'failed' ELSE status END,
       CASE status WHEN 'completed' THEN 'succeeded' WHEN 'cancelled' THEN 'cancelled'
            WHEN 'failed' THEN 'failed' WHEN 'running' THEN 'failed' ELSE NULL END,
       'inline',
       result_preview,artifact_id,
       CASE WHEN status='running' THEN coalesce(error_code,'migration_incomplete') ELSE error_code END,
       started_at,
       CASE WHEN status='running' THEN coalesce(finished_at,started_at) ELSE finished_at END,
       CASE WHEN status='running' THEN coalesce(duration_ms,0) ELSE duration_ms END
FROM tool_invocations_v6;
DROP TABLE tool_invocations_v6;
CREATE INDEX idx_tool_invocations_run ON tool_invocations(run_id,sequence);

CREATE TABLE permission_decisions (
  id TEXT PRIMARY KEY,
  invocation_id TEXT NOT NULL REFERENCES tool_invocations(id) ON DELETE CASCADE,
  behavior TEXT NOT NULL CHECK(behavior IN ('allow','ask','deny')),
  source TEXT NOT NULL,
  reason TEXT NOT NULL,
  rule_json TEXT CHECK(rule_json IS NULL OR json_valid(rule_json)),
  classifier_json TEXT CHECK(classifier_json IS NULL OR json_valid(classifier_json)),
  decided_by TEXT,
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_permission_decisions_invocation ON permission_decisions(invocation_id,created_at);
CREATE TABLE permission_rules (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
  behavior TEXT NOT NULL CHECK(behavior IN ('allow','ask','deny')),
  tool TEXT NOT NULL,
  constraints_json TEXT NOT NULL CHECK(json_valid(constraints_json)),
  source TEXT NOT NULL CHECK(source='session'),
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_permission_rules_session ON permission_rules(session_id,tool);
CREATE TABLE tool_discoveries (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  tool_name TEXT NOT NULL,
  catalog_generation INTEGER NOT NULL CHECK(catalog_generation>=1),
  created_at TEXT NOT NULL,
  PRIMARY KEY(session_id,tool_name)
) STRICT;
CREATE TABLE tool_result_replacements (
  tool_call_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  invocation_id TEXT REFERENCES tool_invocations(id) ON DELETE SET NULL,
  delivery_status TEXT NOT NULL CHECK(delivery_status IN ('artifact','truncated','delivery_failed')),
  replacement_json TEXT NOT NULL CHECK(json_valid(replacement_json)),
  created_at TEXT NOT NULL
) STRICT;
PRAGMA user_version=7;
COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
"""


_UPGRADE_SECOND_STEP = """
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN IMMEDIATE;

ALTER TABLE work_items RENAME TO work_items_v7;
CREATE TABLE work_items (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','waiting_input','completed','failed','cancelled','interrupted','stopped')),
  position INTEGER NOT NULL CHECK(position>=0),
  parent_work_item_id TEXT REFERENCES work_items(id) ON DELETE SET NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
INSERT INTO work_items SELECT * FROM work_items_v7;
DROP TABLE work_items_v7;
CREATE INDEX idx_work_items_session_position ON work_items(session_id,status,position);

ALTER TABLE runs RENAME TO runs_v7;
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','waiting_approval','waiting_input','completed','failed','cancelled','interrupted','stopped')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd>=0),
  error_code TEXT,
  error_message TEXT,
  parent_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  resume_from_step_id TEXT,
  stop_reason TEXT CHECK(stop_reason IS NULL OR stop_reason IN ('max_tool_rounds','max_tool_calls','max_duration','max_tokens','max_budget_usd','repeated_tool_call'))
) STRICT;
INSERT INTO runs SELECT * FROM runs_v7;
DROP TABLE runs_v7;
CREATE INDEX idx_runs_session_started ON runs(session_id,started_at);
CREATE INDEX idx_runs_work_item ON runs(work_item_id,started_at);

ALTER TABLE run_steps RENAME TO run_steps_v7;
CREATE TABLE run_steps (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  kind TEXT NOT NULL CHECK(kind IN ('model','tool','approval')),
  status TEXT NOT NULL CHECK(status IN ('running','waiting_approval','waiting_input','completed','failed','cancelled')),
  checkpoint_json TEXT CHECK(checkpoint_json IS NULL OR json_valid(checkpoint_json)),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT,
  UNIQUE(run_id,ordinal)
) STRICT;
INSERT INTO run_steps SELECT * FROM run_steps_v7;
DROP TABLE run_steps_v7;
CREATE INDEX idx_run_steps_run ON run_steps(run_id,ordinal);

ALTER TABLE run_events RENAME TO run_events_v7;
CREATE TABLE run_events (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  event_id TEXT NOT NULL UNIQUE,
  trace_id TEXT NOT NULL,
  event_kind TEXT NOT NULL CHECK(event_kind IN ('queued','thinking','text_delta','tool_queued','tool_running','tool_progress','tool_permission','tool_completed','tool_cancelled','budget_updated','limit_reached','budget_extended','waiting_approval','waiting_input','completed','failed','cancelled','stopped')),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,sequence)
) STRICT;
INSERT INTO run_events SELECT * FROM run_events_v7;
DROP TABLE run_events_v7;
CREATE INDEX idx_run_events_run ON run_events(run_id,sequence);

ALTER TABLE actions RENAME TO actions_v7;
CREATE TABLE actions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  action_type TEXT NOT NULL CHECK(action_type IN ('file_edit','file_create','notebook_edit','worktree_create','worktree_exit','command','web_search','web_fetch','mcp_connect','mcp_call','credential_access')),
  status TEXT NOT NULL CHECK(status IN ('pending','approved','running','completed','failed','rejected','cancelled')),
  result_kind TEXT,
  summary TEXT NOT NULL,
  request_json TEXT NOT NULL CHECK(json_valid(request_json)),
  result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
  risk_level TEXT,
  risk_reason TEXT,
  rollback TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  approved_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  reversed_at TEXT,
  decided_at TEXT,
  import_id TEXT REFERENCES lifecycle_imports(id) ON DELETE SET NULL,
  historical_only INTEGER NOT NULL DEFAULT 0 CHECK(historical_only IN (0,1)),
  requires_reapproval INTEGER NOT NULL DEFAULT 0 CHECK(requires_reapproval IN (0,1))
) STRICT;
INSERT INTO actions SELECT * FROM actions_v7;
DROP TABLE actions_v7;
CREATE INDEX idx_actions_session_created ON actions(session_id,created_at);
CREATE INDEX idx_actions_run_status ON actions(run_id,status);

ALTER TABLE tasks RENAME TO tasks_v7;
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  subject TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  owner TEXT,
  active_form TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
  status TEXT NOT NULL CHECK(status IN ('pending','running','blocked','completed','failed','cancelled')),
  position INTEGER NOT NULL CHECK(position>=0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
INSERT INTO tasks(id,session_id,run_id,subject,description,status,position,created_at,updated_at)
SELECT id,session_id,run_id,text,'',status,position,created_at,updated_at FROM tasks_v7;
DROP TABLE tasks_v7;
CREATE TABLE task_dependencies (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  blocked_by_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY(task_id,blocked_by_task_id),
  CHECK(task_id<>blocked_by_task_id)
) STRICT;

ALTER TABLE tool_invocations RENAME TO tool_invocations_v7;
CREATE TABLE tool_invocations (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  tool_call_id TEXT NOT NULL,
  name TEXT NOT NULL,
  spec_json TEXT NOT NULL CHECK(json_valid(spec_json)),
  capabilities_json TEXT NOT NULL CHECK(json_valid(capabilities_json)),
  resolved_policy_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(resolved_policy_json)),
  arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
  status TEXT NOT NULL CHECK(status IN ('received','validating','authorizing','queued','running','waiting_approval','waiting_input','completed','failed','cancelled')),
  execution_status TEXT CHECK(execution_status IS NULL OR execution_status IN ('succeeded','failed','denied','cancelled')),
  delivery_status TEXT CHECK(delivery_status IS NULL OR delivery_status IN ('inline','artifact','truncated','delivery_failed')),
  timings_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(timings_json)),
  result_preview TEXT,
  artifact_id TEXT,
  error_code TEXT,
  pause_kind TEXT CHECK(pause_kind IS NULL OR pause_kind IN ('approval','user_input')),
  pause_request_id TEXT,
  continuation_json TEXT CHECK(continuation_json IS NULL OR json_valid(continuation_json)),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
  UNIQUE(run_id,sequence)
) STRICT;
INSERT INTO tool_invocations(
 id,run_id,session_id,sequence,tool_call_id,name,spec_json,capabilities_json,
 resolved_policy_json,arguments_json,status,execution_status,delivery_status,
 timings_json,result_preview,artifact_id,error_code,started_at,finished_at,duration_ms
)
SELECT id,run_id,session_id,sequence,tool_call_id,name,spec_json,capabilities_json,
 resolved_policy_json,arguments_json,
 CASE WHEN execution_status='pending_approval' THEN 'waiting_approval' ELSE status END,
 CASE WHEN execution_status='pending_approval' THEN NULL ELSE execution_status END,
 delivery_status,timings_json,result_preview,artifact_id,error_code,started_at,
 CASE WHEN execution_status='pending_approval' THEN NULL ELSE finished_at END,
 CASE WHEN execution_status='pending_approval' THEN NULL ELSE duration_ms END
FROM tool_invocations_v7;
DROP TABLE tool_invocations_v7;
CREATE INDEX idx_tool_invocations_run ON tool_invocations(run_id,sequence);

CREATE TABLE tool_input_requests (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  invocation_id TEXT NOT NULL REFERENCES tool_invocations(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('pending','answered','cancelled')),
  questions_json TEXT NOT NULL CHECK(json_valid(questions_json)),
  answers_json TEXT CHECK(answers_json IS NULL OR json_valid(answers_json)),
  resume_data_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(resume_data_json)),
  created_at TEXT NOT NULL,
  answered_at TEXT,
  UNIQUE(invocation_id)
) STRICT;
CREATE INDEX idx_tool_input_requests_session ON tool_input_requests(session_id,status,created_at);
CREATE TABLE session_worktrees (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  branch TEXT NOT NULL,
  base_commit TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
  status TEXT NOT NULL CHECK(status IN ('active','kept','removed','invalid')),
  created_at TEXT NOT NULL,
  exited_at TEXT,
  UNIQUE(session_id,path)
) STRICT;
CREATE UNIQUE INDEX idx_session_worktree_active ON session_worktrees(session_id) WHERE active=1;

PRAGMA user_version=8;
COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
"""
