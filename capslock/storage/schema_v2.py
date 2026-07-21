"""Fresh v2 workspace and user-memory schemas."""

WORKSPACE_APPLICATION_ID = 0x434C4B32  # CLK2
MEMORY_APPLICATION_ID = 0x434C4D32  # CLM2
WORKSPACE_SCHEMA_VERSION = 4
MEMORY_SCHEMA_VERSION = 3

WORKSPACE_SCHEMA = """
CREATE TABLE database_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS lifecycle_imports (
  id TEXT PRIMARY KEY,
  archive_id TEXT NOT NULL UNIQUE,
  archive_sha256 TEXT NOT NULL,
  source_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  report_json TEXT NOT NULL CHECK(json_valid(report_json)),
  created_at TEXT NOT NULL,
  completed_at TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS lifecycle_import_items (
  import_id TEXT NOT NULL REFERENCES lifecycle_imports(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target_id TEXT,
  fingerprint TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK(disposition IN ('imported','skipped','remapped','blocked')),
  PRIMARY KEY(import_id,entity_type,source_id)
) STRICT;
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  title_source TEXT NOT NULL CHECK(title_source IN ('pending','first_question','manual')),
  title_updated_at TEXT,
  archived_at TEXT,
  deletion_state TEXT CHECK(deletion_state IS NULL OR deletion_state='deleting')
) STRICT;
CREATE TABLE work_items (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','completed','failed','cancelled','interrupted','stopped')),
  position INTEGER NOT NULL CHECK(position>=0),
  parent_work_item_id TEXT REFERENCES work_items(id) ON DELETE SET NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_work_items_session_position ON work_items(session_id,status,position);
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','waiting_approval','completed','failed','cancelled','interrupted','stopped')),
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
CREATE INDEX idx_runs_session_started ON runs(session_id,started_at);
CREATE INDEX idx_runs_work_item ON runs(work_item_id,started_at);
CREATE TABLE run_steps (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  kind TEXT NOT NULL CHECK(kind IN ('model','tool','approval')),
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
  checkpoint_json TEXT CHECK(checkpoint_json IS NULL OR json_valid(checkpoint_json)),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT,
  UNIQUE(run_id,ordinal)
) STRICT;
CREATE INDEX idx_run_steps_run ON run_steps(run_id,ordinal);
CREATE TABLE run_events (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  event_kind TEXT NOT NULL CHECK(event_kind IN ('queued','thinking','text_delta','tool_running','tool_completed','budget_updated','limit_reached','budget_extended','waiting_approval','completed','failed','cancelled','stopped')),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,sequence)
) STRICT;
CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_messages_session ON messages(session_id,id);
CREATE TABLE actions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  action_type TEXT NOT NULL CHECK(action_type IN ('file_edit','file_create','command','web_search','web_fetch','mcp_connect','mcp_call')),
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
CREATE INDEX idx_actions_session_created ON actions(session_id,created_at);
CREATE INDEX idx_actions_run_status ON actions(run_id,status);
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','running','blocked','completed','failed','cancelled')),
  position INTEGER NOT NULL CHECK(position>=0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE sources (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  excerpt TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  suspicious INTEGER NOT NULL DEFAULT 0 CHECK(suspicious IN (0,1))
) STRICT;
CREATE TABLE tool_calls (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
  ok INTEGER NOT NULL CHECK(ok IN (0,1)),
  result_summary TEXT NOT NULL,
  duration_ms INTEGER NOT NULL CHECK(duration_ms>=0)
) STRICT;
CREATE TABLE citations (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  citation_id TEXT NOT NULL,
  path TEXT NOT NULL,
  start_line INTEGER NOT NULL CHECK(start_line>=1),
  end_line INTEGER NOT NULL CHECK(end_line>=start_line)
) STRICT;
CREATE TABLE workspace_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
CREATE TABLE skill_settings (
  name TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE routing_decisions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  role TEXT NOT NULL CHECK(role IN ('reasoning','fast','embedding','vision')),
  candidates_json TEXT NOT NULL CHECK(json_valid(candidates_json)),
  selected_profile TEXT,
  reason_json TEXT NOT NULL CHECK(json_valid(reason_json)),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,sequence)
) STRICT;
CREATE TABLE model_calls (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  routing_decision_id INTEGER REFERENCES routing_decisions(id) ON DELETE SET NULL,
  role TEXT NOT NULL CHECK(role IN ('reasoning','fast','embedding','vision')),
  profile TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  attempt INTEGER NOT NULL CHECK(attempt>=1),
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  data_policy TEXT NOT NULL,
  fallback_from TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd>=0),
  error_code TEXT,
  error_message TEXT
) STRICT;
CREATE INDEX idx_model_calls_run ON model_calls(run_id,started_at);
CREATE INDEX idx_model_calls_model ON model_calls(provider,model,started_at);
CREATE TABLE budget_decisions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  scope TEXT NOT NULL CHECK(scope IN ('run','session')),
  limit_type TEXT NOT NULL CHECK(limit_type IN ('tokens','cost_usd')),
  current_value REAL NOT NULL CHECK(current_value>=0),
  reserved_value REAL NOT NULL CHECK(reserved_value>=0),
  limit_value REAL NOT NULL CHECK(limit_value>=0),
  decision TEXT NOT NULL CHECK(decision IN ('allowed','denied','hard_stop')),
  profile TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS run_governance (
  run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  root_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK(mode IN ('interactive','exec')),
  limits_json TEXT NOT NULL CHECK(json_valid(limits_json)),
  tool_rounds INTEGER NOT NULL DEFAULT 0 CHECK(tool_rounds>=0),
  tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(tool_calls>=0),
  elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK(elapsed_ms>=0),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd>=0),
  extensions INTEGER NOT NULL DEFAULT 0 CHECK(extensions>=0),
  history_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(history_json)),
  stop_reason TEXT CHECK(stop_reason IS NULL OR stop_reason IN ('max_tool_rounds','max_tool_calls','max_duration','max_tokens','max_budget_usd','repeated_tool_call')),
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS tool_call_attempts (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  round_index INTEGER NOT NULL CHECK(round_index>=1),
  name TEXT NOT NULL,
  arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
  fingerprint TEXT NOT NULL,
  ok INTEGER CHECK(ok IS NULL OR ok IN (0,1)),
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
  created_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(run_id,sequence)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_tool_call_attempts_run ON tool_call_attempts(run_id,sequence);
CREATE VIRTUAL TABLE session_search USING fts5(
  session_id UNINDEXED,
  kind UNINDEXED,
  content,
  created_at UNINDEXED,
  tokenize='unicode61'
);
"""

MEMORY_SCHEMA = """
CREATE TABLE database_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
CREATE TABLE lifecycle_imports (
  id TEXT PRIMARY KEY,
  archive_id TEXT NOT NULL UNIQUE,
  archive_sha256 TEXT NOT NULL,
  source_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  report_json TEXT NOT NULL CHECK(json_valid(report_json)),
  created_at TEXT NOT NULL,
  completed_at TEXT
) STRICT;
CREATE TABLE lifecycle_import_items (
  import_id TEXT NOT NULL REFERENCES lifecycle_imports(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target_id TEXT,
  fingerprint TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK(disposition IN ('imported','skipped','remapped','blocked')),
  PRIMARY KEY(import_id,entity_type,source_id)
) STRICT;
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK(scope IN ('global','workspace','session')),
  workspace_key TEXT,
  session_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('active','forgotten','purged')),
  current_revision INTEGER,
  origin TEXT NOT NULL CHECK(origin IN ('manual','imported','reviewed','automatic')),
  source_valid INTEGER NOT NULL DEFAULT 1 CHECK(source_valid IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  purged_at TEXT,
  CHECK(
    (scope='global' AND workspace_key IS NULL AND session_id IS NULL) OR
    (scope='workspace' AND workspace_key IS NOT NULL AND session_id IS NULL) OR
    (scope='session' AND workspace_key IS NOT NULL AND session_id IS NOT NULL)
  ),
  CHECK((status='purged' AND current_revision IS NULL) OR status!='purged')
) STRICT;
CREATE INDEX idx_memories_scope ON memories(scope,workspace_key,session_id,status);
CREATE TABLE memory_revisions (
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK(revision>=1),
  operation TEXT NOT NULL CHECK(operation IN ('create','edit','forget','undo','import','adopt')),
  content TEXT NOT NULL,
  memory_type TEXT NOT NULL CHECK(memory_type IN ('fact','preference','decision','todo','note')),
  source_kind TEXT NOT NULL,
  source_ref TEXT,
  confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
  expires_at TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id,revision)
) STRICT;
CREATE TABLE memory_workspace_settings (
  workspace_key TEXT PRIMARY KEY,
  write_enabled INTEGER NOT NULL DEFAULT 1 CHECK(write_enabled IN (0,1)),
  policy TEXT NOT NULL DEFAULT 'review' CHECK(policy IN ('off','review','automatic')),
  recall_enabled INTEGER NOT NULL DEFAULT 1 CHECK(recall_enabled IN (0,1)),
  embedding_backend TEXT NOT NULL DEFAULT 'off' CHECK(embedding_backend IN ('off','fastembed','local_http','external')),
  embedding_model TEXT,
  embedding_endpoint TEXT,
  embedding_provider TEXT,
  embedding_data_policy TEXT,
  embedding_consent_id INTEGER
) STRICT;
CREATE TABLE memory_extractions (
  id TEXT PRIMARY KEY,
  workspace_key TEXT NOT NULL,
  session_id TEXT NOT NULL,
  source_run_id TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  policy TEXT NOT NULL CHECK(policy IN ('off','review','automatic')),
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count>=0),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  error_code TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
) STRICT;
CREATE TABLE memory_candidates (
  id TEXT PRIMARY KEY,
  extraction_id TEXT NOT NULL REFERENCES memory_extractions(id) ON DELETE CASCADE,
  content TEXT,
  memory_type TEXT NOT NULL CHECK(memory_type IN ('fact','preference','decision','todo','note')),
  scope TEXT NOT NULL CHECK(scope IN ('global','workspace','session')),
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
CREATE INDEX idx_memory_candidates_queue ON memory_candidates(workspace_key,session_id,status,created_at);
CREATE TABLE memory_sources (
  id INTEGER PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  source_ref TEXT,
  extraction_id TEXT REFERENCES memory_extractions(id) ON DELETE SET NULL,
  workspace_key TEXT,
  session_id TEXT,
  run_id TEXT,
  valid INTEGER NOT NULL DEFAULT 1 CHECK(valid IN (0,1)),
  created_at TEXT NOT NULL,
  invalidated_at TEXT,
  UNIQUE(memory_id,source_kind,source_ref,extraction_id)
) STRICT;
CREATE TABLE memory_embeddings (
  memory_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  backend TEXT NOT NULL CHECK(backend IN ('fastembed','local_http','external')),
  model TEXT NOT NULL,
  dimensions INTEGER NOT NULL CHECK(dimensions>0),
  vector BLOB NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id,revision,backend,model),
  FOREIGN KEY(memory_id,revision) REFERENCES memory_revisions(memory_id,revision) ON DELETE CASCADE
) STRICT;
CREATE TABLE memory_recalls (
  run_id TEXT PRIMARY KEY,
  workspace_key TEXT NOT NULL,
  session_id TEXT NOT NULL,
  query_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE memory_recall_items (
  run_id TEXT NOT NULL REFERENCES memory_recalls(run_id) ON DELETE CASCADE,
  memory_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  score REAL NOT NULL,
  lexical_rank INTEGER,
  semantic_rank INTEGER,
  reasons_json TEXT NOT NULL CHECK(json_valid(reasons_json)),
  PRIMARY KEY(run_id,memory_id),
  FOREIGN KEY(memory_id,revision) REFERENCES memory_revisions(memory_id,revision) ON DELETE CASCADE
) STRICT;
CREATE TABLE memory_accesses (
  memory_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  workspace_key TEXT NOT NULL,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  accessed_at TEXT NOT NULL,
  PRIMARY KEY(memory_id,revision,workspace_key,session_id,run_id),
  FOREIGN KEY(memory_id,revision) REFERENCES memory_revisions(memory_id,revision) ON DELETE CASCADE
) STRICT;
CREATE TABLE memory_audit (
  id INTEGER PRIMARY KEY,
  memory_id TEXT,
  operation TEXT NOT NULL,
  scope TEXT,
  workspace_key TEXT,
  session_id TEXT,
  revision INTEGER,
  detail TEXT,
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE embedding_consents (
  id INTEGER PRIMARY KEY,
  workspace_key TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  data_policy TEXT NOT NULL,
  fields_json TEXT NOT NULL CHECK(json_valid(fields_json)),
  record_count INTEGER NOT NULL CHECK(record_count>=0),
  byte_count INTEGER NOT NULL CHECK(byte_count>=0),
  content_hash TEXT NOT NULL,
  confirmed_at TEXT NOT NULL,
  revoked_at TEXT
) STRICT;
CREATE INDEX idx_embedding_consents_workspace ON embedding_consents(workspace_key,confirmed_at);
CREATE TABLE embedding_requests (
  id INTEGER PRIMARY KEY,
  consent_id INTEGER NOT NULL REFERENCES embedding_consents(id) ON DELETE RESTRICT,
  workspace_key TEXT NOT NULL,
  run_id TEXT,
  operation TEXT NOT NULL CHECK(operation IN ('rebuild','recall')),
  record_count INTEGER NOT NULL CHECK(record_count>=0),
  byte_count INTEGER NOT NULL CHECK(byte_count>=0),
  duration_ms INTEGER NOT NULL CHECK(duration_ms>=0),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd>=0),
  status TEXT NOT NULL CHECK(status IN ('completed','failed')),
  error_code TEXT,
  created_at TEXT NOT NULL
) STRICT;
CREATE VIRTUAL TABLE memory_fts USING fts5(
  memory_id UNINDEXED,
  revision UNINDEXED,
  content,
  tokenize='unicode61'
);
"""


WORKSPACE_MIGRATIONS = {
    1: """
CREATE TABLE routing_decisions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  role TEXT NOT NULL CHECK(role IN ('reasoning','fast','embedding','vision')),
  candidates_json TEXT NOT NULL CHECK(json_valid(candidates_json)),
  selected_profile TEXT,
  reason_json TEXT NOT NULL CHECK(json_valid(reason_json)),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,sequence)
) STRICT;
CREATE TABLE model_calls (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  routing_decision_id INTEGER REFERENCES routing_decisions(id) ON DELETE SET NULL,
  role TEXT NOT NULL CHECK(role IN ('reasoning','fast','embedding','vision')),
  profile TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  attempt INTEGER NOT NULL CHECK(attempt>=1),
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  data_policy TEXT NOT NULL,
  fallback_from TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd>=0),
  error_code TEXT,
  error_message TEXT
) STRICT;
CREATE INDEX idx_model_calls_run ON model_calls(run_id,started_at);
CREATE INDEX idx_model_calls_model ON model_calls(provider,model,started_at);
CREATE TABLE budget_decisions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  scope TEXT NOT NULL CHECK(scope IN ('run','session')),
  limit_type TEXT NOT NULL CHECK(limit_type IN ('tokens','cost_usd')),
  current_value REAL NOT NULL CHECK(current_value>=0),
  reserved_value REAL NOT NULL CHECK(reserved_value>=0),
  limit_value REAL NOT NULL CHECK(limit_value>=0),
  decision TEXT NOT NULL CHECK(decision IN ('allowed','denied','hard_stop')),
  profile TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
""",
    2: """
CREATE TABLE IF NOT EXISTS lifecycle_imports (
  id TEXT PRIMARY KEY,
  archive_id TEXT NOT NULL UNIQUE,
  archive_sha256 TEXT NOT NULL,
  source_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  report_json TEXT NOT NULL CHECK(json_valid(report_json)),
  created_at TEXT NOT NULL,
  completed_at TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS lifecycle_import_items (
  import_id TEXT NOT NULL REFERENCES lifecycle_imports(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target_id TEXT,
  fingerprint TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK(disposition IN ('imported','skipped','remapped','blocked')),
  PRIMARY KEY(import_id,entity_type,source_id)
) STRICT;
""",
    3: """
CREATE TABLE work_items_v4 (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','completed','failed','cancelled','interrupted','stopped')),
  position INTEGER NOT NULL CHECK(position>=0),
  parent_work_item_id TEXT REFERENCES work_items_v4(id) ON DELETE SET NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE runs_v4 (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  work_item_id TEXT NOT NULL REFERENCES work_items_v4(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','waiting_approval','completed','failed','cancelled','interrupted','stopped')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd>=0),
  error_code TEXT,
  error_message TEXT,
  parent_run_id TEXT REFERENCES runs_v4(id) ON DELETE SET NULL,
  resume_from_step_id TEXT,
  stop_reason TEXT CHECK(stop_reason IS NULL OR stop_reason IN ('max_tool_rounds','max_tool_calls','max_duration','max_tokens','max_budget_usd','repeated_tool_call'))
) STRICT;
CREATE TABLE run_events_v4 (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs_v4(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  event_kind TEXT NOT NULL CHECK(event_kind IN ('queued','thinking','text_delta','tool_running','tool_completed','budget_updated','limit_reached','budget_extended','waiting_approval','completed','failed','cancelled','stopped')),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,sequence)
) STRICT;
INSERT INTO work_items_v4 SELECT * FROM work_items;
INSERT INTO runs_v4(id,session_id,work_item_id,question,status,started_at,finished_at,duration_ms,input_tokens,output_tokens,cost_usd,error_code,error_message,parent_run_id,resume_from_step_id)
SELECT id,session_id,work_item_id,question,status,started_at,finished_at,duration_ms,input_tokens,output_tokens,cost_usd,error_code,error_message,parent_run_id,resume_from_step_id FROM runs;
INSERT INTO run_events_v4 SELECT * FROM run_events;
DROP TABLE run_events;
DROP TABLE runs;
DROP TABLE work_items;
ALTER TABLE work_items_v4 RENAME TO work_items;
ALTER TABLE runs_v4 RENAME TO runs;
ALTER TABLE run_events_v4 RENAME TO run_events;
CREATE INDEX idx_work_items_session_position ON work_items(session_id,status,position);
CREATE INDEX idx_runs_session_started ON runs(session_id,started_at);
CREATE INDEX idx_runs_work_item ON runs(work_item_id,started_at);
CREATE TABLE IF NOT EXISTS run_governance (
  run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  root_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK(mode IN ('interactive','exec')),
  limits_json TEXT NOT NULL CHECK(json_valid(limits_json)),
  tool_rounds INTEGER NOT NULL DEFAULT 0 CHECK(tool_rounds>=0),
  tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(tool_calls>=0),
  elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK(elapsed_ms>=0),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
  cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd>=0),
  extensions INTEGER NOT NULL DEFAULT 0 CHECK(extensions>=0),
  history_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(history_json)),
  stop_reason TEXT CHECK(stop_reason IS NULL OR stop_reason IN ('max_tool_rounds','max_tool_calls','max_duration','max_tokens','max_budget_usd','repeated_tool_call')),
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS tool_call_attempts (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  round_index INTEGER NOT NULL CHECK(round_index>=1),
  name TEXT NOT NULL,
  arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
  fingerprint TEXT NOT NULL,
  ok INTEGER CHECK(ok IS NULL OR ok IN (0,1)),
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
  created_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(run_id,sequence)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_tool_call_attempts_run ON tool_call_attempts(run_id,sequence);
INSERT OR IGNORE INTO run_governance(run_id,root_run_id,mode,limits_json,tool_rounds,tool_calls,elapsed_ms,input_tokens,output_tokens,cost_usd,updated_at)
SELECT id,id,'interactive','{"max_tool_rounds":32,"max_tool_calls":null,"max_duration_seconds":null,"max_tokens":null,"max_budget_usd":null}',
       (SELECT count(*) FROM run_steps s WHERE s.run_id=runs.id AND s.kind='model' AND s.status='completed' AND instr(coalesce(s.checkpoint_json,''),'tool_calls')>0),
       (SELECT count(*) FROM tool_calls t WHERE t.run_id=runs.id),coalesce(duration_ms,0),input_tokens,output_tokens,cost_usd,started_at
FROM runs;
""",
}


MEMORY_MIGRATIONS = {
    1: """
ALTER TABLE memory_workspace_settings RENAME TO memory_workspace_settings_v1;
CREATE TABLE memory_workspace_settings (
  workspace_key TEXT PRIMARY KEY,
  write_enabled INTEGER NOT NULL DEFAULT 1 CHECK(write_enabled IN (0,1)),
  policy TEXT NOT NULL DEFAULT 'review' CHECK(policy IN ('off','review','automatic')),
  recall_enabled INTEGER NOT NULL DEFAULT 1 CHECK(recall_enabled IN (0,1)),
  embedding_backend TEXT NOT NULL DEFAULT 'off' CHECK(embedding_backend IN ('off','fastembed','local_http','external')),
  embedding_model TEXT,
  embedding_endpoint TEXT,
  embedding_provider TEXT,
  embedding_data_policy TEXT,
  embedding_consent_id INTEGER
) STRICT;
INSERT INTO memory_workspace_settings(workspace_key,write_enabled,policy,recall_enabled,embedding_backend,embedding_model,embedding_endpoint)
SELECT workspace_key,write_enabled,policy,recall_enabled,embedding_backend,embedding_model,embedding_endpoint FROM memory_workspace_settings_v1;
DROP TABLE memory_workspace_settings_v1;
ALTER TABLE memory_embeddings RENAME TO memory_embeddings_v1;
CREATE TABLE memory_embeddings (
  memory_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  backend TEXT NOT NULL CHECK(backend IN ('fastembed','local_http','external')),
  model TEXT NOT NULL,
  dimensions INTEGER NOT NULL CHECK(dimensions>0),
  vector BLOB NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id,revision,backend,model),
  FOREIGN KEY(memory_id,revision) REFERENCES memory_revisions(memory_id,revision) ON DELETE CASCADE
) STRICT;
INSERT INTO memory_embeddings SELECT * FROM memory_embeddings_v1;
DROP TABLE memory_embeddings_v1;
CREATE TABLE embedding_consents (
  id INTEGER PRIMARY KEY,
  workspace_key TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  data_policy TEXT NOT NULL,
  fields_json TEXT NOT NULL CHECK(json_valid(fields_json)),
  record_count INTEGER NOT NULL CHECK(record_count>=0),
  byte_count INTEGER NOT NULL CHECK(byte_count>=0),
  content_hash TEXT NOT NULL,
  confirmed_at TEXT NOT NULL,
  revoked_at TEXT
) STRICT;
CREATE INDEX idx_embedding_consents_workspace ON embedding_consents(workspace_key,confirmed_at);
CREATE TABLE embedding_requests (
  id INTEGER PRIMARY KEY,
  consent_id INTEGER NOT NULL REFERENCES embedding_consents(id) ON DELETE RESTRICT,
  workspace_key TEXT NOT NULL,
  run_id TEXT,
  operation TEXT NOT NULL CHECK(operation IN ('rebuild','recall')),
  record_count INTEGER NOT NULL CHECK(record_count>=0),
  byte_count INTEGER NOT NULL CHECK(byte_count>=0),
  duration_ms INTEGER NOT NULL CHECK(duration_ms>=0),
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
  cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd>=0),
  status TEXT NOT NULL CHECK(status IN ('completed','failed')),
  error_code TEXT,
  created_at TEXT NOT NULL
) STRICT;
""",
    2: """
CREATE TABLE IF NOT EXISTS lifecycle_imports (
  id TEXT PRIMARY KEY,
  archive_id TEXT NOT NULL UNIQUE,
  archive_sha256 TEXT NOT NULL,
  source_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  report_json TEXT NOT NULL CHECK(json_valid(report_json)),
  created_at TEXT NOT NULL,
  completed_at TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS lifecycle_import_items (
  import_id TEXT NOT NULL REFERENCES lifecycle_imports(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target_id TEXT,
  fingerprint TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK(disposition IN ('imported','skipped','remapped','blocked')),
  PRIMARY KEY(import_id,entity_type,source_id)
) STRICT;
""",
}
