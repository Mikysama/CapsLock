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
    if source_version not in {6, 7, 8, 9, 10, 11, 12, 13}:
        raise ValueError(f"unsupported workspace upgrade source: {source_version}")
    checkpoint = await connection.execute("PRAGMA wal_checkpoint(FULL)")
    await checkpoint.close()
    await connection.commit()
    backup = (
        path.parent
        / "backups"
        / (
            f"capslock-v{source_version}-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            + ".sqlite3"
        )
    )
    await asyncio.to_thread(_backup, path, backup)
    try:
        if source_version == 6:
            await connection.executescript(_UPGRADE_FIRST_STEP)
        if source_version in {6, 7}:
            await connection.executescript(_UPGRADE_SECOND_STEP)
        if source_version in {6, 7, 8}:
            await connection.executescript(_UPGRADE_THIRD_STEP)
        if source_version in {6, 7, 8, 9}:
            await connection.executescript(_UPGRADE_WORKSPACE_TEN)
        if source_version in {6, 7, 8, 9, 10}:
            await connection.executescript(_UPGRADE_WORKSPACE_ELEVEN)
        if source_version in {6, 7, 8, 9, 10, 11}:
            await connection.executescript(_UPGRADE_WORKSPACE_TWELVE)
        if source_version in {6, 7, 8, 9, 10, 11, 12}:
            await connection.executescript(_UPGRADE_WORKSPACE_THIRTEEN)
        await connection.executescript(_UPGRADE_WORKSPACE_FOURTEEN)
    except BaseException:
        await connection.rollback()
        raise
    return backup


async def upgrade_memory_schema(
    path: Path,
    connection: aiosqlite.Connection,
    *,
    source_version: int | None = None,
) -> Path:
    """Backup and transactionally migrate the user memory database to v4."""
    if source_version is None:
        row = await (await connection.execute("PRAGMA user_version")).fetchone()
        source_version = int(row[0])
    if source_version != 3:
        raise ValueError(f"unsupported memory upgrade source: {source_version}")
    checkpoint = await connection.execute("PRAGMA wal_checkpoint(FULL)")
    await checkpoint.close()
    await connection.commit()
    backup = (
        path.parent
        / "backups"
        / (
            f"memory-v{source_version}-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            + ".sqlite3"
        )
    )
    await asyncio.to_thread(_backup, path, backup)
    try:
        await connection.executescript(_UPGRADE_MEMORY_CURRENT)
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


_UPGRADE_THIRD_STEP = """
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN IMMEDIATE;

ALTER TABLE work_items ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent'
  CHECK(kind IN ('agent','local_command','side_question','session_seed'));
ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent'
  CHECK(kind IN ('agent','local_command','side_question','session_seed'));
ALTER TABLE context_compactions ADD COLUMN focus_instructions TEXT;

ALTER TABLE actions RENAME TO actions_v8;
CREATE TABLE actions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  action_type TEXT NOT NULL CHECK(action_type IN ('file_edit','file_create','notebook_edit','worktree_create','worktree_exit','command','web_search','web_fetch','mcp_connect','mcp_call','credential_access','session_rewind')),
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
INSERT INTO actions SELECT * FROM actions_v8;
DROP TABLE actions_v8;
CREATE INDEX idx_actions_session_created ON actions(session_id,created_at);
CREATE INDEX idx_actions_run_status ON actions(run_id,status);

CREATE TABLE session_lineage (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  parent_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  target_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  derivation_kind TEXT NOT NULL CHECK(derivation_kind IN ('branch','rewind')),
  created_at TEXT NOT NULL,
  CHECK(session_id<>parent_session_id)
) STRICT;
CREATE INDEX idx_session_lineage_parent ON session_lineage(parent_session_id,created_at);
CREATE TABLE session_context_state (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  active_compaction_id TEXT REFERENCES context_compactions(id) ON DELETE SET NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE context_snapshots (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  compaction_id TEXT REFERENCES context_compactions(id) ON DELETE SET NULL,
  system_tokens INTEGER NOT NULL DEFAULT 0 CHECK(system_tokens>=0),
  tool_tokens INTEGER NOT NULL DEFAULT 0 CHECK(tool_tokens>=0),
  message_tokens INTEGER NOT NULL DEFAULT 0 CHECK(message_tokens>=0),
  memory_tokens INTEGER NOT NULL DEFAULT 0 CHECK(memory_tokens>=0),
  compaction_tokens INTEGER NOT NULL DEFAULT 0 CHECK(compaction_tokens>=0),
  total_tokens INTEGER NOT NULL CHECK(total_tokens>=0),
  input_budget INTEGER NOT NULL CHECK(input_budget>0),
  trigger_tokens INTEGER NOT NULL CHECK(trigger_tokens>=0),
  stable INTEGER NOT NULL DEFAULT 1 CHECK(stable IN (0,1)),
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_context_snapshots_session ON context_snapshots(session_id,created_at);

PRAGMA user_version=9;
COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
"""


_UPGRADE_WORKSPACE_TEN = """
BEGIN IMMEDIATE;
ALTER TABLE context_compactions ADD COLUMN memory_revision_digest TEXT NOT NULL DEFAULT '';
PRAGMA user_version=10;
COMMIT;
"""


_UPGRADE_WORKSPACE_ELEVEN = """
BEGIN IMMEDIATE;
ALTER TABLE permission_decisions ADD COLUMN reason_code TEXT NOT NULL DEFAULT 'legacy_decision';
ALTER TABLE permission_decisions ADD COLUMN mode TEXT NOT NULL DEFAULT 'approve_for_me'
 CHECK(mode IN ('full_access','approve_for_me','ask_for_approval'));
ALTER TABLE permission_decisions ADD COLUMN arguments_sha256 TEXT NOT NULL DEFAULT '';
ALTER TABLE permission_decisions ADD COLUMN suggestions_json TEXT NOT NULL DEFAULT '[]'
 CHECK(json_valid(suggestions_json));
ALTER TABLE permission_rules ADD COLUMN matcher_version INTEGER NOT NULL DEFAULT 1
 CHECK(matcher_version IN (1,2));
CREATE TABLE permission_requests (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  invocation_id TEXT NOT NULL UNIQUE REFERENCES tool_invocations(id) ON DELETE CASCADE,
  tool TEXT NOT NULL,
  arguments_sha256 TEXT NOT NULL,
  reason TEXT NOT NULL,
  suggestions_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(suggestions_json)),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','cancelled')),
  choice TEXT CHECK(choice IN ('approve_once','approve_session','approve_local','reject')),
  selected_update_json TEXT CHECK(selected_update_json IS NULL OR json_valid(selected_update_json)),
  feedback TEXT,
  result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
  created_at TEXT NOT NULL,
  decided_at TEXT
) STRICT;
CREATE INDEX idx_permission_requests_session ON permission_requests(session_id,status,created_at);
CREATE TABLE permission_grants (
  id TEXT PRIMARY KEY,
  permission_request_id TEXT NOT NULL UNIQUE REFERENCES permission_requests(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  invocation_id TEXT NOT NULL UNIQUE REFERENCES tool_invocations(id) ON DELETE CASCADE,
  tool TEXT NOT NULL,
  arguments_sha256 TEXT NOT NULL,
  consumed_at TEXT,
  created_at TEXT NOT NULL
) STRICT;
PRAGMA user_version=11;
COMMIT;
"""


_UPGRADE_WORKSPACE_TWELVE = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS session_plans (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  objective TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','awaiting_approval','approved','implementing','implemented','implementation_failed','rejected','cancelled')),
  entry_source TEXT NOT NULL CHECK(entry_source IN ('slash','model','resume','branch','rewind')),
  base_permission_mode TEXT NOT NULL CHECK(base_permission_mode IN ('full_access','approve_for_me','ask_for_approval')),
  current_revision_id TEXT,
  parent_plan_id TEXT REFERENCES session_plans(id) ON DELETE SET NULL,
  mirror_relative_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_session_plans_current ON session_plans(session_id,status,updated_at);
CREATE TABLE IF NOT EXISTS plan_revisions (
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES session_plans(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK(ordinal>=1),
  content TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('initial','model','editor','branch','migration')),
  created_by_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  UNIQUE(plan_id,ordinal),
  UNIQUE(plan_id,sha256)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_plan_revisions_plan ON plan_revisions(plan_id,ordinal);
CREATE TABLE IF NOT EXISTS plan_requests (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  plan_id TEXT REFERENCES session_plans(id) ON DELETE CASCADE,
  revision_id TEXT REFERENCES plan_revisions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('enter','submit')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','feedback','rejected','cancelled')),
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
  invocation_id TEXT UNIQUE REFERENCES tool_invocations(id) ON DELETE CASCADE,
  objective TEXT,
  choice TEXT CHECK(choice IS NULL OR choice IN ('enter','implement','feedback','reject')),
  feedback TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  CHECK((kind='enter' AND revision_id IS NULL) OR (kind='submit' AND plan_id IS NOT NULL AND revision_id IS NOT NULL))
) STRICT;
CREATE INDEX IF NOT EXISTS idx_plan_requests_session ON plan_requests(session_id,status,created_at);
CREATE TABLE IF NOT EXISTS plan_implementations (
  plan_id TEXT PRIMARY KEY REFERENCES session_plans(id) ON DELETE CASCADE,
  revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
  request_id TEXT NOT NULL UNIQUE REFERENCES plan_requests(id) ON DELETE CASCADE,
  work_item_id TEXT NOT NULL UNIQUE REFERENCES work_items(id) ON DELETE CASCADE,
  run_id TEXT UNIQUE REFERENCES runs(id) ON DELETE SET NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
PRAGMA user_version=12;
COMMIT;
"""


_UPGRADE_WORKSPACE_THIRTEEN = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS performance_spans (
  id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
  parent_span_id TEXT REFERENCES performance_spans(id) ON DELETE SET NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ok','error','cancelled')),
  duration_ms REAL NOT NULL CHECK(duration_ms>=0),
  attributes_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(attributes_json)),
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_performance_spans_trace ON performance_spans(trace_id,created_at);
CREATE INDEX IF NOT EXISTS idx_performance_spans_name ON performance_spans(category,name,created_at);
PRAGMA user_version=13;
COMMIT;
"""


_UPGRADE_WORKSPACE_FOURTEEN = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS agent_mailbox (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  parent_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sender TEXT NOT NULL CHECK(sender IN ('parent','child','system')),
  recipient TEXT NOT NULL CHECK(recipient IN ('parent','child')),
  message_kind TEXT NOT NULL CHECK(message_kind IN ('instruction','question','response','progress','artifact_offer','cancel')),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  payload_sha256 TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','delivered','acknowledged','expired')),
  created_at TEXT NOT NULL,
  expires_at TEXT,
  delivered_at TEXT,
  acknowledged_at TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS idx_agent_mailbox_delivery ON agent_mailbox(task_id,recipient,status,created_at);
PRAGMA user_version=14;
COMMIT;
"""


_UPGRADE_MEMORY_CURRENT = """
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN IMMEDIATE;

ALTER TABLE memories RENAME TO memories_v3;
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK(scope IN ('global','workspace','session','agent')),
  workspace_key TEXT,
  session_id TEXT,
  namespace TEXT,
  status TEXT NOT NULL CHECK(status IN ('active','forgotten','purged')),
  current_revision INTEGER,
  origin TEXT NOT NULL CHECK(origin IN ('manual','imported','reviewed','automatic')),
  source_valid INTEGER NOT NULL DEFAULT 1 CHECK(source_valid IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  purged_at TEXT,
  CHECK(
    (scope='global' AND workspace_key IS NULL AND session_id IS NULL AND namespace IS NULL) OR
    (scope='workspace' AND workspace_key IS NOT NULL AND session_id IS NULL AND namespace IS NULL) OR
    (scope='session' AND workspace_key IS NOT NULL AND session_id IS NOT NULL AND namespace IS NULL) OR
    (scope='agent' AND workspace_key IS NOT NULL AND session_id IS NULL AND namespace IS NOT NULL)
  ),
  CHECK((status='purged' AND current_revision IS NULL) OR status!='purged')
) STRICT;
INSERT INTO memories(id,scope,workspace_key,session_id,status,current_revision,origin,
 source_valid,created_at,updated_at,purged_at)
 SELECT id,scope,workspace_key,session_id,status,current_revision,origin,
 source_valid,created_at,updated_at,purged_at FROM memories_v3;
DROP TABLE memories_v3;
CREATE INDEX idx_memories_scope ON memories(scope,workspace_key,session_id,status);

ALTER TABLE memory_revisions RENAME TO memory_revisions_previous;
CREATE TABLE memory_revisions (
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK(revision>=1),
  operation TEXT NOT NULL CHECK(operation IN ('create','edit','forget','undo','import','adopt')),
  content TEXT NOT NULL,
  memory_type TEXT NOT NULL CHECK(memory_type IN ('fact','preference','decision','todo','note','project','temporary')),
  source_kind TEXT NOT NULL,
  source_ref TEXT,
  confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
  expires_at TEXT,
  subject TEXT,
  durability TEXT NOT NULL DEFAULT 'durable' CHECK(durability IN ('temporary','session','project','durable')),
  why TEXT,
  how_to_apply TEXT,
  last_verified_at TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id,revision)
) STRICT;
INSERT INTO memory_revisions(memory_id,revision,operation,content,memory_type,
 source_kind,source_ref,confidence,expires_at,created_at)
 SELECT memory_id,revision,operation,content,memory_type,source_kind,source_ref,
 confidence,expires_at,created_at FROM memory_revisions_previous;
DROP TABLE memory_revisions_previous;

ALTER TABLE memory_workspace_settings ADD COLUMN capture_enabled INTEGER NOT NULL DEFAULT 1
 CHECK(capture_enabled IN (0,1));
ALTER TABLE memory_workspace_settings ADD COLUMN manual_write_enabled INTEGER NOT NULL DEFAULT 1
 CHECK(manual_write_enabled IN (0,1));
ALTER TABLE memory_workspace_settings ADD COLUMN maintenance_enabled INTEGER NOT NULL DEFAULT 1
 CHECK(maintenance_enabled IN (0,1));
UPDATE memory_workspace_settings
 SET capture_enabled=write_enabled,
     manual_write_enabled=write_enabled,
     maintenance_enabled=CASE WHEN write_enabled=0 THEN 0 ELSE 1 END,
     policy='automatic';

ALTER TABLE memory_extractions ADD COLUMN envelope_json TEXT NOT NULL DEFAULT '{}'
 CHECK(json_valid(envelope_json));
ALTER TABLE memory_candidates RENAME TO memory_candidates_previous;
CREATE TABLE memory_candidates (
  id TEXT PRIMARY KEY,
  extraction_id TEXT NOT NULL REFERENCES memory_extractions(id) ON DELETE CASCADE,
  content TEXT,
  memory_type TEXT NOT NULL CHECK(memory_type IN ('fact','preference','decision','todo','note','project','temporary')),
  scope TEXT NOT NULL CHECK(scope IN ('global','workspace','session','agent')),
  namespace TEXT,
  subject TEXT,
  durability TEXT NOT NULL DEFAULT 'durable' CHECK(durability IN ('temporary','session','project','durable')),
  why TEXT,
  how_to_apply TEXT,
  workspace_key TEXT NOT NULL,
  session_id TEXT NOT NULL,
  source_run_id TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
  status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','duplicate','conflict','purged')),
  relation TEXT NOT NULL CHECK(relation IN ('new','duplicate','conflict')),
  related_memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
  risk_flags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(risk_flags_json)),
  adopted_memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT
) STRICT;
INSERT INTO memory_candidates(id,extraction_id,content,memory_type,scope,workspace_key,
 session_id,source_run_id,confidence,status,relation,related_memory_id,risk_flags_json,
 adopted_memory_id,created_at,decided_at)
 SELECT id,extraction_id,content,memory_type,scope,workspace_key,session_id,
 source_run_id,confidence,status,relation,related_memory_id,risk_flags_json,
 adopted_memory_id,created_at,decided_at FROM memory_candidates_previous;
DROP TABLE memory_candidates_previous;
CREATE INDEX idx_memory_candidates_queue ON memory_candidates(workspace_key,session_id,status,created_at);
ALTER TABLE memory_sources ADD COLUMN message_id TEXT;
ALTER TABLE memory_sources ADD COLUMN evidence_id TEXT;
ALTER TABLE memory_sources ADD COLUMN quote TEXT;
ALTER TABLE memory_sources ADD COLUMN direct INTEGER NOT NULL DEFAULT 0 CHECK(direct IN (0,1));
ALTER TABLE memory_sources ADD COLUMN verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1));
ALTER TABLE memory_recall_items ADD COLUMN cosine REAL;
ALTER TABLE memory_recall_items ADD COLUMN retrieval_score REAL NOT NULL DEFAULT 0;
ALTER TABLE memory_recall_items ADD COLUMN selected_reason TEXT;
ALTER TABLE memory_recall_items ADD COLUMN filter_reason TEXT;

CREATE TABLE memory_candidate_sources (
  id INTEGER PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES memory_candidates(id) ON DELETE CASCADE,
  message_id TEXT,
  evidence_id TEXT,
  quote TEXT NOT NULL,
  direct INTEGER NOT NULL DEFAULT 0 CHECK(direct IN (0,1)),
  verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
  created_at TEXT NOT NULL,
  CHECK(message_id IS NOT NULL OR evidence_id IS NOT NULL)
) STRICT;
CREATE TABLE memory_relations (
  id INTEGER PRIMARY KEY,
  source_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  target_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  relation TEXT NOT NULL CHECK(relation IN ('duplicate','conflict','supersedes')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected')),
  confidence REAL NOT NULL DEFAULT 1 CHECK(confidence>=0 AND confidence<=1),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE(source_memory_id,target_memory_id,relation),
  CHECK(source_memory_id<>target_memory_id)
) STRICT;
CREATE INDEX idx_memory_relations_status ON memory_relations(status,relation,created_at);
CREATE TABLE memory_jobs (
  id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL CHECK(job_type IN ('extract_run','consolidate_workspace','promote_agent_memory')),
  workspace_key TEXT NOT NULL,
  session_id TEXT,
  run_id TEXT,
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','failed')),
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0 AND attempt_count<=3),
  available_at TEXT NOT NULL,
  error_code TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT
) STRICT;
CREATE INDEX idx_memory_jobs_ready ON memory_jobs(status,available_at,created_at);
CREATE TABLE memory_review_proposals (
  id TEXT PRIMARY KEY,
  workspace_key TEXT NOT NULL,
  proposal_type TEXT NOT NULL CHECK(proposal_type IN ('near_duplicate','conflict','rewrite','instruction_promotion')),
  memory_ids_json TEXT NOT NULL CHECK(json_valid(memory_ids_json)),
  proposed_content TEXT,
  confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
  payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
  job_id TEXT REFERENCES memory_jobs(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT
) STRICT;
CREATE INDEX idx_memory_review_queue ON memory_review_proposals(workspace_key,status,created_at);
CREATE TABLE memory_maintenance_state (
  workspace_key TEXT PRIMARY KEY,
  last_consolidated_at TEXT,
  completed_sessions_at_last_run INTEGER NOT NULL DEFAULT 0 CHECK(completed_sessions_at_last_run>=0),
  last_job_id TEXT REFERENCES memory_jobs(id) ON DELETE SET NULL
) STRICT;

PRAGMA user_version=4;
COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
"""
