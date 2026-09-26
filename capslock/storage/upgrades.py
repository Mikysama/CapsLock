"""Transactional, backup-first workspace schema upgrades."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    if source_version not in {
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
    }:
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
        if source_version in {6, 7, 8, 9, 10, 11, 12, 13}:
            await connection.executescript(_UPGRADE_WORKSPACE_FOURTEEN)
        if source_version < 15:
            await connection.executescript(_UPGRADE_WORKSPACE_FIFTEEN)
        if source_version < 16:
            artifact_columns = {
                str(row[1])
                for row in await (
                    await connection.execute("PRAGMA table_info(tool_artifacts)")
                ).fetchall()
            }
            if "index_content" not in artifact_columns:
                await connection.execute(
                    """ALTER TABLE tool_artifacts ADD COLUMN index_content INTEGER
                       NOT NULL DEFAULT 1 CHECK(index_content IN (0,1))"""
                )
                await connection.commit()
            await connection.executescript(_UPGRADE_WORKSPACE_SIXTEEN)
        if source_version < 17:
            compaction_columns = {
                str(row[1])
                for row in await (
                    await connection.execute("PRAGMA table_info(context_compactions)")
                ).fetchall()
            }
            if "summary_policy_digest" not in compaction_columns:
                await connection.execute(
                    """ALTER TABLE context_compactions ADD COLUMN summary_policy_digest
                       TEXT NOT NULL DEFAULT ''"""
                )
            if "result_tokens" not in compaction_columns:
                await connection.execute(
                    """ALTER TABLE context_compactions ADD COLUMN result_tokens
                       INTEGER NOT NULL DEFAULT 0 CHECK(result_tokens>=0)"""
                )
            if "quality_status" not in compaction_columns:
                await connection.execute(
                    """ALTER TABLE context_compactions ADD COLUMN quality_status
                       TEXT NOT NULL DEFAULT 'legacy'
                       CHECK(quality_status IN
                         ('legacy','ok','degraded','target_unreachable'))"""
                )
            await connection.commit()
            await connection.executescript(_UPGRADE_WORKSPACE_SEVENTEEN)
        if source_version < 18:
            agent_task_columns = {
                str(row[1])
                for row in await (
                    await connection.execute("PRAGMA table_info(agent_tasks)")
                ).fetchall()
            }
            if "owner_session_id" not in agent_task_columns:
                await connection.executescript(_UPGRADE_WORKSPACE_EIGHTEEN)
            else:
                # Test fixtures and development snapshots can be structurally newer
                # than their user_version.  Keep the upgrade idempotent in that case.
                await connection.execute("PRAGMA user_version=18")
                await connection.commit()
        migrated_contract_rows = await (
            await connection.execute(
                "SELECT id,contract_json FROM agent_tasks WHERE contract_sha256=''"
            )
        ).fetchall()
        for row in migrated_contract_rows:
            canonical = json.dumps(
                json.loads(str(row[1])), sort_keys=True, ensure_ascii=False
            )
            await connection.execute(
                "UPDATE agent_tasks SET contract_sha256=? WHERE id=?",
                (hashlib.sha256(canonical.encode("utf-8")).hexdigest(), str(row[0])),
            )
        if migrated_contract_rows:
            await connection.commit()
        if source_version < 19:
            await connection.executescript(_UPGRADE_WORKSPACE_NINETEEN)
        if source_version < 20:
            obsolete_tables = {
                str(row[0])
                for row in await (
                    await connection.execute(
                        """SELECT name FROM sqlite_master WHERE type='table' AND
                           name IN ('tool_result_replacements','context_snapshots',
                                    'citations','agent_capabilities')"""
                    )
                ).fetchall()
            }
            if obsolete_tables:
                if len(obsolete_tables) != 4:
                    raise ValueError("workspace v20 legacy tables are incomplete")
                await _validate_workspace_twenty(connection)
                await connection.executescript(_UPGRADE_WORKSPACE_TWENTY)
            else:
                invocation_columns = {
                    str(row[1])
                    for row in await (
                        await connection.execute("PRAGMA table_info(tool_invocations)")
                    ).fetchall()
                }
                tool_call_columns = {
                    str(row[1])
                    for row in await (
                        await connection.execute("PRAGMA table_info(tool_calls)")
                    ).fetchall()
                }
                if "delivered_result_json" not in invocation_columns or (
                    "invocation_id" not in tool_call_columns
                ):
                    raise ValueError("workspace v20 canonical columns are incomplete")
                await connection.execute("PRAGMA user_version=20")
                await connection.commit()
        if source_version < 21:
            await connection.execute("BEGIN IMMEDIATE")
            additions = {
                "sessions": {"model_profile": "TEXT"},
                "model_calls": {
                    "cached_input_tokens": "INTEGER",
                    "reasoning_tokens": "INTEGER",
                    "usage_source": "TEXT",
                    "request_id": "TEXT",
                    "first_token_ms": "INTEGER",
                    "retry_delay_ms": "INTEGER",
                    "output_started": "INTEGER",
                    "price_snapshot_json": "TEXT CHECK(price_snapshot_json IS NULL OR json_valid(price_snapshot_json))",
                },
            }
            for table, fields in additions.items():
                columns = {
                    str(row[1])
                    for row in await (
                        await connection.execute(f"PRAGMA table_info({table})")
                    ).fetchall()
                }
                for field, definition in fields.items():
                    if field not in columns:
                        await connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {field} {definition}"
                        )
            await connection.execute("PRAGMA user_version=21")
            await _validate_integrity(connection, "workspace")
            await connection.commit()
        if source_version < 22:
            await _upgrade_workspace_twenty_two(connection)
        await _validate_integrity(connection, "workspace")
    except BaseException as exc:
        await connection.rollback()
        # Recovery must not depend on a best-effort diagnostic file being writable.
        await _restore_backup(connection, backup)
        try:
            await asyncio.to_thread(
                _write_migration_report, backup, source_version, exc
            )
        except OSError as report_error:
            exc.add_note(f"migration report unavailable: {report_error}")
        raise
    return backup


async def upgrade_memory_schema(
    path: Path,
    connection: aiosqlite.Connection,
    *,
    source_version: int | None = None,
) -> Path:
    """Backup and transactionally migrate the user memory database to v6."""
    if source_version is None:
        row = await (await connection.execute("PRAGMA user_version")).fetchone()
        source_version = int(row[0])
    if source_version not in {3, 4, 5}:
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
        if source_version == 3:
            await connection.executescript(_UPGRADE_MEMORY_CURRENT)
        if source_version < 5:
            await connection.executescript(_UPGRADE_MEMORY_FIVE)
        if source_version < 6:
            await connection.executescript(_UPGRADE_MEMORY_SIX)
        await _validate_integrity(connection, "memory")
    except BaseException:
        await connection.rollback()
        await _restore_backup(connection, backup)
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


def _write_migration_report(
    backup_path: Path, source_version: int, error: BaseException
) -> None:
    report = backup_path.with_name(f"{backup_path.stem}-migration-report.json")
    report.write_text(
        json.dumps(
            {
                "source_version": source_version,
                "backup": str(backup_path),
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "created_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    report.chmod(0o600)


async def _restore_backup(connection: aiosqlite.Connection, backup_path: Path) -> None:
    """Restore the pre-migration image into the still-owned connection."""
    source = await aiosqlite.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        await source.backup(connection)
    finally:
        await source.close()
    await connection.execute("PRAGMA foreign_keys=ON")
    await connection.commit()


async def _validate_integrity(connection: aiosqlite.Connection, label: str) -> None:
    integrity = await (await connection.execute("PRAGMA integrity_check")).fetchall()
    if [str(row[0]) for row in integrity] != ["ok"]:
        raise ValueError(f"{label} database integrity check failed")
    foreign = await (await connection.execute("PRAGMA foreign_key_check")).fetchall()
    if foreign:
        raise ValueError(f"{label} database foreign key check failed")


async def _validate_workspace_twenty(connection: aiosqlite.Connection) -> None:
    replacement = await (
        await connection.execute(
            """SELECT r.tool_call_id FROM tool_result_replacements r
               LEFT JOIN tool_invocations i ON i.id=r.invocation_id
               WHERE r.invocation_id IS NULL OR i.id IS NULL
                  OR i.tool_call_id<>r.tool_call_id
                  OR i.session_id<>r.session_id
               LIMIT 1"""
        )
    ).fetchone()
    if replacement is not None:
        raise ValueError("tool result replacement has no matching invocation")
    duplicate = await (
        await connection.execute(
            """SELECT invocation_id FROM tool_result_replacements
               GROUP BY invocation_id HAVING count(*)<>1 LIMIT 1"""
        )
    ).fetchone()
    if duplicate is not None:
        raise ValueError("multiple tool result replacements match one invocation")

    capability_rows = await (
        await connection.execute(
            """SELECT t.id,t.contract_json,t.contract_sha256,c.ordinal,c.capability_json
               FROM agent_tasks t LEFT JOIN agent_capabilities c ON c.task_id=t.id
               ORDER BY t.id,c.ordinal"""
        )
    ).fetchall()
    contracts: dict[str, tuple[list[object], list[object]]] = {}
    for row in capability_rows:
        task_id = str(row[0])
        if task_id not in contracts:
            document = json.loads(str(row[1]))
            canonical = json.dumps(document, sort_keys=True, ensure_ascii=False)
            if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != str(row[2]):
                raise ValueError("agent task contract checksum is invalid")
            expected = document.get("capabilities", [])
            if not isinstance(expected, list):
                raise ValueError("agent task contract capabilities are invalid")
            contracts[task_id] = (expected, [])
        if row[3] is not None:
            contracts[task_id][1].append(json.loads(str(row[4])))
    if any(expected != stored for expected, stored in contracts.values()):
        raise ValueError("agent capability rows do not match task contracts")

    rows = await (
        await connection.execute(
            "SELECT run_id,citation_id,path,start_line,end_line FROM citations ORDER BY run_id,id"
        )
    ).fetchall()
    by_run: dict[str, list[tuple[object, ...]]] = {}
    for row in rows:
        by_run.setdefault(str(row[0]), []).append(tuple(row[1:]))
    for run_id, stored in by_run.items():
        events = await (
            await connection.execute(
                """SELECT payload_json FROM run_events
                   WHERE run_id=? AND event_kind='completed' ORDER BY sequence DESC""",
                (run_id,),
            )
        ).fetchall()
        found = False
        for event in events:
            payload = json.loads(str(event[0]))
            citations = (
                payload.get("citations", []) if isinstance(payload, dict) else []
            )
            canonical = [
                (
                    item.get("id", item.get("citation_id")),
                    item.get("path"),
                    item.get("start_line"),
                    item.get("end_line"),
                )
                for item in citations
                if isinstance(item, dict)
                and item.get("path") is not None
                and item.get("start_line") is not None
                and item.get("end_line") is not None
            ]
            if canonical == stored:
                found = True
                break
        if not found:
            raise ValueError(
                f"citation rows are not present in terminal event: {run_id}"
            )


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


_UPGRADE_WORKSPACE_FIFTEEN = """
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN IMMEDIATE;
ALTER TABLE work_items RENAME TO work_items_v14;
CREATE TABLE work_items (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'agent' CHECK(kind IN ('agent','init','local_command','side_question','session_seed')),
  status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','waiting_input','completed','failed','cancelled','interrupted','stopped')),
  position INTEGER NOT NULL CHECK(position>=0),
  parent_work_item_id TEXT REFERENCES work_items(id) ON DELETE SET NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
INSERT INTO work_items(
  id,session_id,question,kind,status,position,parent_work_item_id,error,created_at,updated_at
)
SELECT
  id,session_id,question,kind,status,position,parent_work_item_id,error,created_at,updated_at
FROM work_items_v14;
DROP TABLE work_items_v14;
CREATE INDEX idx_work_items_session_position ON work_items(session_id,status,position);

ALTER TABLE runs RENAME TO runs_v14;
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'agent' CHECK(kind IN ('agent','init','local_command','side_question','session_seed')),
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
INSERT INTO runs(
  id,session_id,work_item_id,question,kind,status,started_at,finished_at,duration_ms,
  input_tokens,output_tokens,cost_usd,error_code,error_message,parent_run_id,
  resume_from_step_id,stop_reason
)
SELECT
  id,session_id,work_item_id,question,kind,status,started_at,finished_at,duration_ms,
  input_tokens,output_tokens,cost_usd,error_code,error_message,parent_run_id,
  resume_from_step_id,stop_reason
FROM runs_v14;
DROP TABLE runs_v14;
CREATE INDEX idx_runs_session_started ON runs(session_id,started_at);
CREATE INDEX idx_runs_work_item ON runs(work_item_id,started_at);
PRAGMA user_version=15;
COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
"""


_UPGRADE_WORKSPACE_SIXTEEN = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS context_summary_segments (
  id TEXT PRIMARY KEY,
  source_digest TEXT NOT NULL,
  model_profile TEXT NOT NULL,
  summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
  source_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(source_refs_json)),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  created_at TEXT NOT NULL,
  UNIQUE(source_digest,model_profile)
) STRICT;
CREATE TABLE IF NOT EXISTS episodic_documents (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('message','tool_result','artifact')),
  source_id TEXT NOT NULL,
  chunk_ordinal INTEGER NOT NULL CHECK(chunk_ordinal>=0),
  content TEXT NOT NULL,
  artifact_id TEXT REFERENCES tool_artifacts(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  UNIQUE(session_id,source_kind,source_id,chunk_ordinal)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_episodic_documents_session ON episodic_documents(session_id,created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS episodic_fts USING fts5(
  content,
  content='episodic_documents',
  content_rowid='id',
  tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS episodic_documents_ai AFTER INSERT ON episodic_documents BEGIN
  INSERT INTO episodic_fts(rowid,content) VALUES(new.id,new.content);
END;
CREATE TRIGGER IF NOT EXISTS episodic_documents_ad AFTER DELETE ON episodic_documents BEGIN
  INSERT INTO episodic_fts(episodic_fts,rowid,content) VALUES('delete',old.id,old.content);
END;
CREATE TRIGGER IF NOT EXISTS episodic_documents_au AFTER UPDATE ON episodic_documents BEGIN
  INSERT INTO episodic_fts(episodic_fts,rowid,content) VALUES('delete',old.id,old.content);
  INSERT INTO episodic_fts(rowid,content) VALUES(new.id,new.content);
END;
CREATE TRIGGER IF NOT EXISTS episodic_messages_ai AFTER INSERT ON messages BEGIN
  INSERT OR IGNORE INTO episodic_documents(
    session_id,run_id,source_kind,source_id,chunk_ordinal,content,created_at
  ) VALUES(new.session_id,new.run_id,'message',cast(new.id AS TEXT),0,new.content,new.created_at);
END;
CREATE TRIGGER IF NOT EXISTS episodic_tool_invocations_ai AFTER INSERT ON tool_invocations
WHEN new.result_preview IS NOT NULL BEGIN
  INSERT OR IGNORE INTO episodic_documents(
    session_id,run_id,source_kind,source_id,chunk_ordinal,content,artifact_id,created_at
  ) VALUES(new.session_id,new.run_id,'tool_result',new.id,0,new.result_preview,new.artifact_id,new.started_at);
END;
CREATE TRIGGER IF NOT EXISTS episodic_tool_artifacts_ai AFTER INSERT ON tool_artifacts BEGIN
  INSERT OR IGNORE INTO episodic_documents(
    session_id,run_id,source_kind,source_id,chunk_ordinal,content,artifact_id,created_at
  ) VALUES(
    new.session_id,new.run_id,'artifact',new.id,0,
    CASE
      WHEN new.index_content=0 THEN
        'quarantined artifact metadata: media_type=' || new.media_type ||
        '; size_bytes=' || new.size_bytes || '; sha256=' || new.sha256 ||
        '; content omitted'
      WHEN lower(new.media_type) LIKE 'text/%'
        OR lower(new.media_type) LIKE 'application/%json%'
        OR lower(new.media_type) LIKE 'application/%xml%'
        OR lower(new.media_type) IN (
          'application/javascript','application/x-javascript',
          'application/yaml','application/x-yaml'
        ) THEN new.preview
      ELSE 'binary artifact metadata: media_type=' || new.media_type ||
        '; size_bytes=' || new.size_bytes || '; sha256=' || new.sha256 ||
        '; content omitted'
    END,
    new.id,new.created_at
  );
END;
INSERT OR IGNORE INTO episodic_documents(
  session_id,run_id,source_kind,source_id,chunk_ordinal,content,created_at
)
SELECT session_id,run_id,'message',cast(id AS TEXT),0,content,created_at FROM messages;
INSERT OR IGNORE INTO episodic_documents(
  session_id,run_id,source_kind,source_id,chunk_ordinal,content,artifact_id,created_at
)
SELECT session_id,run_id,'tool_result',id,0,result_preview,artifact_id,started_at
FROM tool_invocations WHERE result_preview IS NOT NULL;
INSERT OR IGNORE INTO episodic_documents(
  session_id,run_id,source_kind,source_id,chunk_ordinal,content,artifact_id,created_at
)
SELECT session_id,run_id,'artifact',id,0,
  CASE
    WHEN index_content=0 THEN
      'quarantined artifact metadata: media_type=' || media_type ||
      '; size_bytes=' || size_bytes || '; sha256=' || sha256 ||
      '; content omitted'
    WHEN lower(media_type) LIKE 'text/%'
      OR lower(media_type) LIKE 'application/%json%'
      OR lower(media_type) LIKE 'application/%xml%'
      OR lower(media_type) IN (
        'application/javascript','application/x-javascript',
        'application/yaml','application/x-yaml'
      ) THEN preview
    ELSE 'binary artifact metadata: media_type=' || media_type ||
      '; size_bytes=' || size_bytes || '; sha256=' || sha256 ||
      '; content omitted'
  END,
  id,created_at FROM tool_artifacts;
PRAGMA user_version=16;
COMMIT;
"""


_UPGRADE_WORKSPACE_SEVENTEEN = """
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
ALTER TABLE context_summary_segments RENAME TO context_summary_segments_v16;
CREATE TABLE context_summary_segments (
  id TEXT PRIMARY KEY,
  source_digest TEXT NOT NULL,
  model_profile TEXT NOT NULL,
  summary_policy_digest TEXT NOT NULL DEFAULT '',
  summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
  source_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(source_refs_json)),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  created_at TEXT NOT NULL,
  UNIQUE(source_digest,model_profile,summary_policy_digest)
) STRICT;
INSERT INTO context_summary_segments(
 id,source_digest,model_profile,summary_policy_digest,summary_json,
 source_refs_json,input_tokens,output_tokens,created_at
) SELECT id,source_digest,model_profile,'',summary_json,
 source_refs_json,input_tokens,output_tokens,created_at
 FROM context_summary_segments_v16;
DROP TABLE context_summary_segments_v16;
PRAGMA user_version=17;
COMMIT;
PRAGMA foreign_keys=ON;
"""


_UPGRADE_WORKSPACE_EIGHTEEN = """
PRAGMA foreign_keys=OFF;
PRAGMA legacy_alter_table=ON;
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS agent_teams (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','stopped')),
  created_by_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  stopped_at TEXT,
  UNIQUE(session_id,name)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_agent_teams_session ON agent_teams(session_id,created_at);
INSERT OR IGNORE INTO agent_teams(id,session_id,name,state,created_by_run_id,created_at)
SELECT 'default:' || s.id,s.id,'default','active',
       (SELECT r.id FROM runs r WHERE r.session_id=s.id ORDER BY r.started_at,r.id LIMIT 1),
       s.created_at
FROM sessions s;

CREATE TABLE IF NOT EXISTS agent_workers (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL REFERENCES agent_teams(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  profile_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(profile_json)),
  workspace_mode TEXT NOT NULL DEFAULT 'snapshot' CHECK(workspace_mode IN ('snapshot','worktree','shared_read')),
  state TEXT NOT NULL DEFAULT 'starting' CHECK(state IN ('starting','idle','running','waiting_approval','interrupted','stopped')),
  persistent INTEGER NOT NULL DEFAULT 1 CHECK(persistent IN (0,1)),
  child_session_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  stopped_at TEXT,
  UNIQUE(team_id,name)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_agent_workers_team ON agent_workers(team_id,state,created_at);
INSERT OR IGNORE INTO agent_workers(id,team_id,name,profile_json,workspace_mode,state,persistent,created_at,updated_at,stopped_at)
SELECT 'legacy:' || t.id,'default:' || r.session_id,'legacy-' || substr(t.id,1,12),'{}','snapshot',
       CASE WHEN t.state IN ('created','running','waiting_approval') THEN 'interrupted' ELSE 'stopped' END,
       0,t.created_at,coalesce(t.finished_at,t.created_at),t.finished_at
FROM agent_tasks t JOIN runs r ON r.id=t.parent_run_id;

ALTER TABLE agent_tasks RENAME TO agent_tasks_v16;
ALTER TABLE agent_workspaces RENAME TO agent_workspaces_v16;
ALTER TABLE agent_capabilities RENAME TO agent_capabilities_v16;
ALTER TABLE agent_messages RENAME TO agent_messages_v16;
ALTER TABLE agent_mailbox RENAME TO agent_mailbox_v16;
ALTER TABLE agent_outputs RENAME TO agent_outputs_v16;
DROP INDEX IF EXISTS idx_agent_tasks_parent;
DROP INDEX IF EXISTS idx_agent_messages_task;
DROP INDEX IF EXISTS idx_agent_mailbox_delivery;

CREATE TABLE agent_tasks (
  id TEXT PRIMARY KEY,
  parent_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  owner_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  team_id TEXT NOT NULL REFERENCES agent_teams(id) ON DELETE CASCADE,
  assigned_worker_id TEXT REFERENCES agent_workers(id) ON DELETE SET NULL,
  plan_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  objective TEXT NOT NULL,
  contract_json TEXT NOT NULL CHECK(json_valid(contract_json)),
  contract_sha256 TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK(state IN ('created','blocked','ready','claimed','running','waiting_approval','completed','failed','cancelled','interrupted')),
  child_run_id TEXT,
  child_workspace TEXT,
  claim_token TEXT,
  claim_expires_at TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
) STRICT;
INSERT INTO agent_tasks(
  id,parent_run_id,owner_session_id,team_id,assigned_worker_id,plan_task_id,
  objective,contract_json,contract_sha256,priority,state,child_run_id,child_workspace,
  claim_token,claim_expires_at,attempt_count,error,created_at,started_at,finished_at)
SELECT t.id,t.parent_run_id,r.session_id,'default:' || r.session_id,'legacy:' || t.id,NULL,
       t.objective,t.contract_json,'',0,
       CASE WHEN t.state IN ('created','running','waiting_approval') THEN 'interrupted' ELSE t.state END,
       t.child_run_id,t.child_workspace,NULL,NULL,0,
       CASE WHEN t.state IN ('created','running','waiting_approval')
            THEN 'interrupted during schema upgrade' ELSE t.error END,
       t.created_at,t.started_at,
       CASE WHEN t.state IN ('created','running','waiting_approval')
            THEN coalesce(t.finished_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ELSE t.finished_at END
FROM agent_tasks_v16 t JOIN runs r ON r.id=t.parent_run_id;
CREATE INDEX idx_agent_tasks_parent ON agent_tasks(parent_run_id,created_at);
CREATE INDEX idx_agent_tasks_session ON agent_tasks(owner_session_id,state,created_at);
CREATE INDEX idx_agent_tasks_team_ready ON agent_tasks(team_id,state,priority DESC,created_at);

CREATE TABLE agent_workspaces (
  task_id TEXT PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
  worker_id TEXT REFERENCES agent_workers(id) ON DELETE SET NULL,
  workspace_mode TEXT NOT NULL DEFAULT 'snapshot' CHECK(workspace_mode IN ('snapshot','worktree','shared_read')),
  path TEXT NOT NULL,
  source_path TEXT NOT NULL,
  base_commit TEXT,
  baseline_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(baseline_json)),
  retained INTEGER NOT NULL DEFAULT 0 CHECK(retained IN (0,1)),
  created_at TEXT NOT NULL,
  cleaned_at TEXT
) STRICT;
INSERT INTO agent_workspaces(task_id,worker_id,workspace_mode,path,source_path,base_commit,
                             baseline_json,retained,created_at,cleaned_at)
SELECT task_id,'legacy:' || task_id,'snapshot',path,source_path,NULL,'{}',retained,created_at,cleaned_at
FROM agent_workspaces_v16;

CREATE TABLE agent_capabilities (
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  capability_json TEXT NOT NULL CHECK(json_valid(capability_json)),
  PRIMARY KEY(task_id,ordinal)
) STRICT;
INSERT INTO agent_capabilities SELECT * FROM agent_capabilities_v16;

CREATE TABLE agent_messages (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  parent_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  team_id TEXT REFERENCES agent_teams(id) ON DELETE CASCADE,
  worker_id TEXT REFERENCES agent_workers(id) ON DELETE SET NULL,
  attempt_id TEXT REFERENCES agent_attempts(id) ON DELETE SET NULL,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  message_kind TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  payload_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(task_id,sequence)
) STRICT;
INSERT INTO agent_messages(
  id,task_id,parent_run_id,team_id,worker_id,attempt_id,sender,recipient,sequence,
  message_kind,payload_json,payload_sha256,created_at)
SELECT m.id,m.task_id,m.parent_run_id,t.team_id,t.assigned_worker_id,NULL,m.sender,m.recipient,
       m.sequence,m.message_kind,m.payload_json,m.payload_sha256,m.created_at
FROM agent_messages_v16 m JOIN agent_tasks t ON t.id=m.task_id;
CREATE INDEX idx_agent_messages_task ON agent_messages(task_id,sequence);

CREATE TABLE agent_mailbox (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  parent_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  team_id TEXT REFERENCES agent_teams(id) ON DELETE CASCADE,
  worker_id TEXT REFERENCES agent_workers(id) ON DELETE SET NULL,
  attempt_id TEXT REFERENCES agent_attempts(id) ON DELETE SET NULL,
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
INSERT INTO agent_mailbox(
  id,task_id,parent_run_id,team_id,worker_id,attempt_id,sender,recipient,message_kind,
  payload_json,payload_sha256,status,created_at,expires_at,delivered_at,acknowledged_at)
SELECT m.id,m.task_id,m.parent_run_id,t.team_id,t.assigned_worker_id,NULL,m.sender,m.recipient,
       m.message_kind,m.payload_json,m.payload_sha256,m.status,m.created_at,m.expires_at,
       m.delivered_at,m.acknowledged_at
FROM agent_mailbox_v16 m JOIN agent_tasks t ON t.id=m.task_id;
CREATE INDEX idx_agent_mailbox_delivery ON agent_mailbox(task_id,recipient,status,created_at);

CREATE TABLE agent_outputs (
  task_id TEXT PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
  attempt_id TEXT REFERENCES agent_attempts(id) ON DELETE SET NULL,
  state TEXT NOT NULL CHECK(state IN ('completed','failed','cancelled','interrupted')),
  output_json TEXT NOT NULL CHECK(json_valid(output_json)),
  verified INTEGER NOT NULL CHECK(verified IN (0,1)),
  output_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
INSERT INTO agent_outputs(task_id,attempt_id,state,output_json,verified,output_sha256,created_at)
SELECT task_id,NULL,state,output_json,verified,output_sha256,created_at FROM agent_outputs_v16;

DROP TABLE agent_outputs_v16;
DROP TABLE agent_mailbox_v16;
DROP TABLE agent_messages_v16;
DROP TABLE agent_capabilities_v16;
DROP TABLE agent_workspaces_v16;
DROP TABLE agent_tasks_v16;

CREATE TABLE IF NOT EXISTS agent_task_dependencies (
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  blocked_by_task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY(task_id,blocked_by_task_id),
  CHECK(task_id<>blocked_by_task_id)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_agent_task_dependencies_blocker ON agent_task_dependencies(blocked_by_task_id,task_id);
CREATE TABLE IF NOT EXISTS agent_attempts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  worker_id TEXT REFERENCES agent_workers(id) ON DELETE SET NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal>=1),
  state TEXT NOT NULL CHECK(state IN ('created','running','suspended','completed','failed','cancelled','interrupted')),
  claim_token TEXT NOT NULL UNIQUE,
  child_run_id TEXT,
  reservation_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(reservation_json)),
  usage_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(usage_json)),
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(task_id,ordinal)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_agent_attempts_task ON agent_attempts(task_id,ordinal);
CREATE TABLE IF NOT EXISTS agent_checkpoints (
  attempt_id TEXT PRIMARY KEY REFERENCES agent_attempts(id) ON DELETE CASCADE,
  contract_sha256 TEXT NOT NULL,
  child_session_id TEXT,
  child_run_id TEXT,
  transcript_cursor TEXT,
  checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(checkpoint_json)),
  resumable INTEGER NOT NULL DEFAULT 0 CHECK(resumable IN (0,1)),
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS agent_budget_ledger (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL REFERENCES agent_teams(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  attempt_id TEXT NOT NULL REFERENCES agent_attempts(id) ON DELETE CASCADE,
  operation TEXT NOT NULL CHECK(operation IN ('reserve','settle','release')),
  amount_json TEXT NOT NULL CHECK(json_valid(amount_json)),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_agent_budget_attempt ON agent_budget_ledger(attempt_id,created_at);
CREATE TABLE IF NOT EXISTS agent_approval_links (
  id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES agent_attempts(id) ON DELETE CASCADE,
  child_action_id TEXT NOT NULL,
  parent_action_id TEXT,
  action_sha256 TEXT NOT NULL,
  contract_sha256 TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','rejected','cancelled')),
  payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE(attempt_id,child_action_id)
) STRICT;
PRAGMA user_version=18;
COMMIT;
PRAGMA legacy_alter_table=OFF;
PRAGMA foreign_keys=ON;
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


_UPGRADE_MEMORY_FIVE = """
BEGIN IMMEDIATE;
ALTER TABLE memories ADD COLUMN owner_session_id TEXT;
ALTER TABLE memories ADD COLUMN project_instance_id TEXT;
ALTER TABLE memory_workspace_settings ADD COLUMN temporary_ttl_days INTEGER NOT NULL DEFAULT 7
 CHECK(temporary_ttl_days BETWEEN 1 AND 365);
ALTER TABLE memory_candidates ADD COLUMN extractor_confidence REAL NOT NULL DEFAULT 0
 CHECK(extractor_confidence>=0 AND extractor_confidence<=1);
ALTER TABLE memory_candidates ADD COLUMN verifier_confidence REAL
 CHECK(verifier_confidence IS NULL OR (verifier_confidence>=0 AND verifier_confidence<=1));
ALTER TABLE memory_candidates ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'unverified'
 CHECK(verification_status IN ('unverified','supported','unsupported','failed'));
ALTER TABLE memory_candidates ADD COLUMN instruction_like INTEGER NOT NULL DEFAULT 0
 CHECK(instruction_like IN (0,1));
ALTER TABLE memory_candidates ADD COLUMN calibration_version TEXT;
CREATE TABLE IF NOT EXISTS memory_extraction_segments (
  id TEXT PRIMARY KEY,
  source_digest TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  response_json TEXT NOT NULL CHECK(json_valid(response_json)),
  source_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(source_refs_json)),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  created_at TEXT NOT NULL,
  UNIQUE(source_digest,model,prompt_version)
) STRICT;
UPDATE memory_candidates SET extractor_confidence=confidence;
UPDATE memories SET owner_session_id=coalesce(
  session_id,
  (SELECT s.session_id FROM memory_sources s
   WHERE s.memory_id=memories.id AND s.session_id IS NOT NULL ORDER BY s.id LIMIT 1)
) WHERE id IN (
  SELECT m.id FROM memories m JOIN memory_revisions r
  ON r.memory_id=m.id AND r.revision=m.current_revision
  WHERE r.durability='session'
);
UPDATE memory_revisions SET expires_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+7 days')
 WHERE durability='temporary' AND expires_at IS NULL;
PRAGMA user_version=5;
COMMIT;
"""


_UPGRADE_WORKSPACE_NINETEEN = """
BEGIN IMMEDIATE;
DROP INDEX IF EXISTS idx_run_steps_run;
DROP INDEX IF EXISTS idx_run_events_run;
DROP INDEX IF EXISTS idx_tool_invocations_run;
DROP INDEX IF EXISTS idx_plan_revisions_plan;
DROP INDEX IF EXISTS idx_tool_call_attempts_run;
DROP INDEX IF EXISTS idx_agent_attempts_task;
DROP INDEX IF EXISTS idx_agent_messages_task;

UPDATE run_steps AS step SET checkpoint_json=NULL
WHERE checkpoint_json IS NOT NULL
  AND status NOT IN ('waiting_approval','waiting_input')
  AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.resume_from_step_id=step.id)
  AND ordinal < (
    SELECT max(newer.ordinal) FROM run_steps newer
    WHERE newer.run_id=step.run_id AND newer.checkpoint_json IS NOT NULL
  );

CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_invocations_call
  ON tool_invocations(run_id,tool_call_id);
CREATE INDEX IF NOT EXISTS idx_tool_invocations_working_set
  ON tool_invocations(session_id,name,status,finished_at DESC,sequence DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_session_order ON tasks(session_id,position,created_at);
CREATE INDEX IF NOT EXISTS idx_sources_session_time ON sources(session_id,fetched_at);
CREATE INDEX IF NOT EXISTS idx_agent_mailbox_worker_delivery
  ON agent_mailbox(worker_id,recipient,status,created_at);
CREATE INDEX IF NOT EXISTS idx_agent_approval_links_parent ON agent_approval_links(parent_action_id);
CREATE INDEX IF NOT EXISTS idx_agent_workspaces_worker_open
  ON agent_workspaces(worker_id,cleaned_at,created_at);
CREATE INDEX IF NOT EXISTS idx_agent_budget_team ON agent_budget_ledger(team_id,created_at);
CREATE INDEX IF NOT EXISTS idx_performance_spans_created ON performance_spans(created_at);
PRAGMA user_version=19;
COMMIT;
"""


_UPGRADE_WORKSPACE_TWENTY = """
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
ALTER TABLE tool_invocations ADD COLUMN delivered_result_json TEXT
  CHECK(delivered_result_json IS NULL OR json_valid(delivered_result_json));
UPDATE tool_invocations SET delivered_result_json=(
  SELECT replacement_json FROM tool_result_replacements r
  WHERE r.invocation_id=tool_invocations.id
) WHERE id IN (
  SELECT invocation_id FROM tool_result_replacements WHERE invocation_id IS NOT NULL
);
ALTER TABLE tool_calls ADD COLUMN invocation_id TEXT
  REFERENCES tool_invocations(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX idx_tool_calls_invocation ON tool_calls(invocation_id);
DROP TABLE tool_result_replacements;
DROP TABLE context_snapshots;
DROP TABLE citations;
DROP TABLE agent_capabilities;
PRAGMA user_version=20;
COMMIT;
PRAGMA foreign_keys=ON;
"""


_UPGRADE_MEMORY_SIX = """
BEGIN IMMEDIATE;
DROP INDEX IF EXISTS idx_memory_jobs_ready;
CREATE INDEX IF NOT EXISTS idx_memory_candidate_sources_candidate
  ON memory_candidate_sources(candidate_id,id);
CREATE INDEX IF NOT EXISTS idx_memory_jobs_claim
  ON memory_jobs(status,workspace_key,job_type,created_at,available_at);
CREATE INDEX IF NOT EXISTS idx_memory_jobs_history
  ON memory_jobs(workspace_key,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_recalls_session_latest
  ON memory_recalls(workspace_key,session_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_accesses_session
  ON memory_accesses(workspace_key,session_id,run_id,memory_id,revision);
PRAGMA user_version=6;
COMMIT;
"""


async def _upgrade_workspace_twenty_two(connection: aiosqlite.Connection) -> None:
    """Rebuild only changed tables in one transaction; preserve their identities."""
    from .schema import WORKSPACE_SCHEMA

    await connection.execute("BEGIN IMMEDIATE")
    columns = {
        str(row[1])
        for row in await (
            await connection.execute("PRAGMA table_info(agent_mailbox)")
        ).fetchall()
    }
    if "recipient_address" not in columns:
        start = WORKSPACE_SCHEMA.index("CREATE TABLE agent_mailbox (")
        end = WORKSPACE_SCHEMA.index("CREATE TABLE mailbox_deliveries (", start)
        definition = WORKSPACE_SCHEMA[start:end]
        await connection.execute(
            "ALTER TABLE agent_mailbox RENAME TO agent_mailbox_v21"
        )
        await connection.execute("DROP INDEX IF EXISTS idx_agent_mailbox_delivery")
        await connection.execute(
            "DROP INDEX IF EXISTS idx_agent_mailbox_worker_delivery"
        )
        for statement in definition.split(";"):
            if statement.strip():
                await connection.execute(statement)
        old_columns = [
            str(row[1])
            for row in await (
                await connection.execute("PRAGMA table_info(agent_mailbox_v21)")
            ).fetchall()
        ]
        names = ",".join(old_columns)
        await connection.execute(
            f"INSERT INTO agent_mailbox({names}) SELECT {names} FROM agent_mailbox_v21"
        )
        await connection.execute("""UPDATE agent_mailbox SET
            sender_address=CASE WHEN sender='parent' THEN
              'session:' || (SELECT owner_session_id FROM agent_tasks WHERE id=agent_mailbox.task_id)
              WHEN sender='child' THEN CASE WHEN worker_id IS NOT NULL THEN 'worker:'||worker_id ELSE 'task:'||task_id END END,
            recipient_address=CASE WHEN recipient='parent' THEN
              'session:' || (SELECT owner_session_id FROM agent_tasks WHERE id=agent_mailbox.task_id)
              ELSE CASE WHEN worker_id IS NOT NULL THEN 'worker:'||worker_id ELSE 'task:'||task_id END END""")
        await connection.execute("DROP TABLE agent_mailbox_v21")
    columns = {
        str(row[1])
        for row in await (
            await connection.execute("PRAGMA table_info(agent_mailbox)")
        ).fetchall()
    }
    if "delivery_suspended" not in columns:
        await connection.execute(
            "ALTER TABLE agent_mailbox ADD COLUMN delivery_suspended INTEGER NOT NULL DEFAULT 0 CHECK(delivery_suspended IN (0,1))"
        )
    start = WORKSPACE_SCHEMA.index("CREATE TABLE mailbox_deliveries (")
    end = WORKSPACE_SCHEMA.index("CREATE TABLE agent_outputs (", start)
    for statement in (
        WORKSPACE_SCHEMA[start:end]
        .replace(
            "CREATE TABLE mailbox_deliveries",
            "CREATE TABLE IF NOT EXISTS mailbox_deliveries",
        )
        .replace("CREATE INDEX idx_mailbox", "CREATE INDEX IF NOT EXISTS idx_mailbox")
        .split(";")
    ):
        if statement.strip():
            await connection.execute(statement)
    schema_row = await (
        await connection.execute("SELECT sql FROM sqlite_master WHERE name='run_steps'")
    ).fetchone()
    if "'mailbox'" not in str(schema_row[0]):
        start = WORKSPACE_SCHEMA.index("CREATE TABLE run_steps (")
        end = WORKSPACE_SCHEMA.index("CREATE TABLE run_events (", start)
        definition = WORKSPACE_SCHEMA[start:end].replace(
            "CREATE TABLE run_steps", "CREATE TABLE run_steps_v22"
        )
        await connection.execute(definition)
        await connection.execute("INSERT INTO run_steps_v22 SELECT * FROM run_steps")
        await connection.execute("DROP TABLE run_steps")
        await connection.execute("ALTER TABLE run_steps_v22 RENAME TO run_steps")
    await connection.execute("PRAGMA user_version=22")
    await _validate_integrity(connection, "workspace")
    await connection.commit()
