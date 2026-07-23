"""Pure portable-import table merging and index rebuilding."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from .errors import LifecycleError
from .specs import REFERENCE_FIELDS


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def merge_tables(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
    tables: tuple[str, ...],
    primary: dict[str, str | tuple[str, ...]],
    import_id: str,
    archive_id: str,
    report: dict[str, Any],
    *,
    domain: str,
    external_maps: dict[str, dict[str, str]] | None = None,
    target_workspace_key: str | None = None,
) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {}
    all_maps = external_maps or {}
    for table in tables:
        records = payload.get(table, [])
        if not isinstance(records, list):
            raise LifecycleError(f"{table} must be a list")
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not columns:
            continue
        key_spec = primary[table]
        keys = (key_spec,) if isinstance(key_spec, str) else key_spec
        table_map: dict[str, str] = {}
        for source in records:
            if not isinstance(source, dict) or any(key not in source for key in keys):
                raise LifecycleError(f"invalid {table} record")
            record = {name: value for name, value in source.items() if name in columns}
            source_id = json.dumps(
                [source[key] for key in keys], separators=(",", ":"), default=str
            )
            rewrite_references(
                table, record, {**all_maps, **maps}, target_workspace_key
            )
            record_fingerprint = fingerprint(record)
            target_values = tuple(record[key] for key in keys)
            where = " AND ".join(f"{key}=?" for key in keys)
            existing = connection.execute(
                f"SELECT * FROM {table} WHERE {where}", target_values
            ).fetchone()
            disposition = "imported"
            if existing is not None:
                existing_value = {name: existing[name] for name in record}
                if fingerprint(existing_value) == record_fingerprint:
                    disposition = "skipped"
                elif len(keys) > 1 or table in {
                    "workspace_settings",
                    "skill_settings",
                    "memory_workspace_settings",
                }:
                    disposition = "blocked"
                else:
                    key = keys[0]
                    target_values = (
                        remapped_id(
                            connection,
                            table,
                            key,
                            archive_id,
                            source_id,
                            target_values[0],
                        ),
                    )
                    record[key] = target_values[0]
                    disposition = "remapped"
            target_id = json.dumps(target_values, separators=(",", ":"), default=str)
            if len(keys) == 1:
                table_map[str(source[keys[0]])] = str(target_values[0])
                target_id = str(target_values[0])
            if disposition not in {"skipped", "blocked"}:
                if table == "actions":
                    record["import_id"] = import_id
                insert_record(connection, table, record)
            report[disposition] += 1
            connection.execute(
                """INSERT OR REPLACE INTO lifecycle_import_items(
                   import_id,entity_type,source_id,target_id,fingerprint,disposition) VALUES(?,?,?,?,?,?)""",
                (
                    import_id,
                    f"{domain}:{table}",
                    source_id,
                    target_id,
                    record_fingerprint,
                    disposition,
                ),
            )
        maps[table] = table_map
    return maps


def rewrite_references(
    table_name: str,
    record: dict[str, Any],
    maps: dict[str, dict[str, str]],
    target_workspace_key: str | None,
) -> None:
    references = dict(REFERENCE_FIELDS)
    if table_name in {"memory_recalls", "memory_recall_items"}:
        references["run_id"] = (
            "memory_recalls" if table_name == "memory_recall_items" else "runs"
        )
    for field, table in references.items():
        value = record.get(field)
        if value is not None and str(value) in maps.get(table, {}):
            record[field] = maps[table][str(value)]
    if (
        target_workspace_key
        and "workspace_key" in record
        and not (table_name == "memories" and record.get("scope") == "global")
    ):
        record["workspace_key"] = target_workspace_key


def normalize_imported_workflow(connection: sqlite3.Connection, import_id: str) -> None:
    def imported(table: str) -> str:
        return f"id IN (SELECT target_id FROM lifecycle_import_items WHERE import_id=? AND entity_type='workspace:{table}' AND disposition IN ('imported','remapped'))"

    connection.execute(
        f"UPDATE work_items SET status='interrupted',error='interrupted during export' WHERE status='running' AND {imported('work_items')}",
        (import_id,),
    )
    connection.execute(
        f"UPDATE runs SET status='interrupted',finished_at=coalesce(finished_at,?),error_code='imported_interrupted',error_message='interrupted during export' WHERE status='running' AND {imported('runs')}",
        (utc_now(), import_id),
    )
    connection.execute(
        f"""INSERT OR IGNORE INTO run_governance(
               run_id,root_run_id,mode,limits_json,tool_rounds,tool_calls,
               elapsed_ms,input_tokens,output_tokens,cost_usd,updated_at)
            SELECT r.id,r.id,'interactive',
                   '{{"max_tool_rounds":32,"max_tool_calls":null,"max_duration_seconds":null,"max_tokens":null,"max_budget_usd":null}}',
                   (SELECT count(*) FROM run_steps s WHERE s.run_id=r.id AND s.kind='model' AND s.status='completed' AND instr(coalesce(s.checkpoint_json,''),'tool_calls')>0),
                   (SELECT count(*) FROM tool_calls t WHERE t.run_id=r.id),
                   coalesce(r.duration_ms,0),r.input_tokens,r.output_tokens,r.cost_usd,?
              FROM runs r WHERE {imported("runs")}""",
        (utc_now(), import_id),
    )
    connection.execute(
        f"UPDATE run_steps SET status='cancelled',finished_at=coalesce(finished_at,?),error='interrupted during export' WHERE status='running' AND {imported('run_steps')}",
        (utc_now(), import_id),
    )
    connection.execute(
        f"UPDATE model_calls SET status='failed',finished_at=coalesce(finished_at,?),error_code='imported_interrupted',error_message='interrupted during export' WHERE status='running' AND {imported('model_calls')}",
        (utc_now(), import_id),
    )
    connection.execute(
        f"UPDATE agent_tasks SET state='interrupted',finished_at=coalesce(finished_at,?),error='interrupted during export',child_workspace=NULL WHERE state IN ('created','running','waiting_approval') AND {imported('agent_tasks')}",
        (utc_now(), import_id),
    )
    connection.execute(
        "UPDATE actions SET historical_only=1,requires_reapproval=0 WHERE import_id=? AND status IN ('completed','failed','rejected','cancelled')",
        (import_id,),
    )
    connection.execute(
        """UPDATE actions SET status='pending',approved_at=NULL,started_at=NULL,
           finished_at=NULL,decided_at=NULL,result_json=NULL,result_kind=NULL,
           error_code=NULL,error_message=NULL,historical_only=0,requires_reapproval=1
           WHERE import_id=? AND status IN ('pending','approved','running')""",
        (import_id,),
    )


def rebuild_session_search(
    connection: sqlite3.Connection, session_ids: set[str]
) -> None:
    for session_id in session_ids:
        connection.execute(
            "DELETE FROM session_search WHERE session_id=?", (session_id,)
        )
        session = connection.execute(
            "SELECT title,created_at FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if session is None:
            continue
        connection.execute(
            "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
            (session_id, "title", session["title"], session["created_at"]),
        )
        connection.executemany(
            "INSERT INTO session_search(session_id,kind,content,created_at) VALUES(?,?,?,?)",
            [
                (session_id, "message", row["content"], row["created_at"])
                for row in connection.execute(
                    "SELECT content,created_at FROM messages WHERE session_id=? ORDER BY id",
                    (session_id,),
                )
            ],
        )


def rebuild_memory_fts(connection: sqlite3.Connection, memory_ids: set[str]) -> None:
    for memory_id in memory_ids:
        connection.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        row = connection.execute(
            """SELECT m.current_revision,r.content FROM memories m
               LEFT JOIN memory_revisions r ON r.memory_id=m.id AND r.revision=m.current_revision
               WHERE m.id=? AND m.status='active'""",
            (memory_id,),
        ).fetchone()
        if row is not None and row[0] is not None and row[1] is not None:
            connection.execute(
                "INSERT INTO memory_fts(memory_id,revision,content) VALUES(?,?,?)",
                (memory_id, row[0], row[1]),
            )


def insert_record(
    connection: sqlite3.Connection, table: str, record: dict[str, Any]
) -> None:
    columns = tuple(record)
    marks = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({marks})",
        tuple(record[name] for name in columns),
    )


def remapped_id(
    connection: sqlite3.Connection,
    table: str,
    key: str,
    archive_id: str,
    source_id: str,
    current: object,
) -> object:
    integer = isinstance(current, int)
    for attempt in range(1000):
        seed = f"{archive_id}:{table}:{source_id}:{attempt}"
        candidate: object = (
            int(hashlib.sha256(seed.encode()).hexdigest()[:14], 16)
            if integer
            else uuid.uuid5(uuid.NAMESPACE_URL, seed).hex
        )
        if (
            connection.execute(
                f"SELECT 1 FROM {table} WHERE {key}=?", (candidate,)
            ).fetchone()
            is None
        ):
            return candidate
    raise LifecycleError(f"could not remap {table} id")


def fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
