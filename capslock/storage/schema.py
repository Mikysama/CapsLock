"""Fresh workspace and user-memory schemas."""

WORKSPACE_APPLICATION_ID = 0x434C4B32  # CLK2
MEMORY_APPLICATION_ID = 0x434C4D32  # CLM2
WORKSPACE_SCHEMA_VERSION = 15
MEMORY_SCHEMA_VERSION = 4

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
  kind TEXT NOT NULL DEFAULT 'agent' CHECK(kind IN ('agent','init','local_command','side_question','session_seed')),
  status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','waiting_input','completed','failed','cancelled','interrupted','stopped')),
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
CREATE INDEX idx_runs_session_started ON runs(session_id,started_at);
CREATE INDEX idx_runs_work_item ON runs(work_item_id,started_at);
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
CREATE INDEX idx_run_steps_run ON run_steps(run_id,ordinal);
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
CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_messages_session ON messages(session_id,id);
CREATE TABLE session_lineage (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  parent_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  target_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  derivation_kind TEXT NOT NULL CHECK(derivation_kind IN ('branch','rewind')),
  created_at TEXT NOT NULL,
  CHECK(session_id<>parent_session_id)
) STRICT;
CREATE INDEX idx_session_lineage_parent ON session_lineage(parent_session_id,created_at);
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
CREATE INDEX idx_actions_session_created ON actions(session_id,created_at);
CREATE INDEX idx_actions_run_status ON actions(run_id,status);
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
CREATE TABLE task_dependencies (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  blocked_by_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY(task_id,blocked_by_task_id),
  CHECK(task_id<>blocked_by_task_id)
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
CREATE TABLE tool_artifacts (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  invocation_id TEXT REFERENCES tool_invocations(id) ON DELETE SET NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK(size_bytes>=0 AND size_bytes<=5242880),
  media_type TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  preview TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(session_id,sha256)
) STRICT;
CREATE INDEX idx_tool_artifacts_session ON tool_artifacts(session_id,created_at);
CREATE TABLE permission_decisions (
  id TEXT PRIMARY KEY,
  invocation_id TEXT NOT NULL REFERENCES tool_invocations(id) ON DELETE CASCADE,
  behavior TEXT NOT NULL CHECK(behavior IN ('allow','ask','deny')),
  source TEXT NOT NULL,
  reason TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('full_access','approve_for_me','ask_for_approval')),
  arguments_sha256 TEXT NOT NULL,
  rule_json TEXT CHECK(rule_json IS NULL OR json_valid(rule_json)),
  classifier_json TEXT CHECK(classifier_json IS NULL OR json_valid(classifier_json)),
  suggestions_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(suggestions_json)),
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
  matcher_version INTEGER NOT NULL DEFAULT 2 CHECK(matcher_version IN (1,2)),
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_permission_rules_session ON permission_rules(session_id,tool);
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
CREATE TABLE session_plans (
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
CREATE INDEX idx_session_plans_current ON session_plans(session_id,status,updated_at);
CREATE TABLE plan_revisions (
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
CREATE INDEX idx_plan_revisions_plan ON plan_revisions(plan_id,ordinal);
CREATE TABLE plan_requests (
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
CREATE INDEX idx_plan_requests_session ON plan_requests(session_id,status,created_at);
CREATE TABLE plan_implementations (
  plan_id TEXT PRIMARY KEY REFERENCES session_plans(id) ON DELETE CASCADE,
  revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
  request_id TEXT NOT NULL UNIQUE REFERENCES plan_requests(id) ON DELETE CASCADE,
  work_item_id TEXT NOT NULL UNIQUE REFERENCES work_items(id) ON DELETE CASCADE,
  run_id TEXT UNIQUE REFERENCES runs(id) ON DELETE SET NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
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
CREATE TABLE context_compactions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  first_message_id INTEGER,
  last_message_id INTEGER,
  summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
  source_compaction_id TEXT REFERENCES context_compactions(id) ON DELETE SET NULL,
  input_tokens INTEGER NOT NULL CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL CHECK(output_tokens>=0),
  source_tokens INTEGER NOT NULL CHECK(source_tokens>=0),
  target_tokens INTEGER NOT NULL CHECK(target_tokens>=0),
  model_profile TEXT NOT NULL,
  source_digest TEXT NOT NULL,
  memory_revision_digest TEXT NOT NULL DEFAULT '',
  focus_instructions TEXT,
  valid INTEGER NOT NULL DEFAULT 1 CHECK(valid IN (0,1)),
  created_at TEXT NOT NULL,
  CHECK(first_message_id IS NULL OR last_message_id IS NULL OR first_message_id<=last_message_id)
) STRICT;
CREATE INDEX idx_context_compactions_session ON context_compactions(session_id,created_at);
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

CREATE TABLE agent_tasks (
  id TEXT PRIMARY KEY,
  parent_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  objective TEXT NOT NULL,
  contract_json TEXT NOT NULL CHECK(json_valid(contract_json)),
  state TEXT NOT NULL CHECK(state IN ('created','running','waiting_approval','completed','failed','cancelled','interrupted')),
  child_run_id TEXT,
  child_workspace TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
) STRICT;
CREATE INDEX idx_agent_tasks_parent ON agent_tasks(parent_run_id,created_at);
CREATE TABLE agent_workspaces (
  task_id TEXT PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  source_path TEXT NOT NULL,
  retained INTEGER NOT NULL DEFAULT 0 CHECK(retained IN (0,1)),
  created_at TEXT NOT NULL,
  cleaned_at TEXT
) STRICT;
CREATE TABLE agent_capabilities (
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  capability_json TEXT NOT NULL CHECK(json_valid(capability_json)),
  PRIMARY KEY(task_id,ordinal)
) STRICT;
CREATE TABLE agent_messages (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  parent_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  message_kind TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  payload_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(task_id,sequence)
) STRICT;
CREATE INDEX idx_agent_messages_task ON agent_messages(task_id,sequence);
CREATE TABLE agent_mailbox (
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
CREATE INDEX idx_agent_mailbox_delivery ON agent_mailbox(task_id,recipient,status,created_at);
CREATE TABLE agent_outputs (
  task_id TEXT PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
  state TEXT NOT NULL CHECK(state IN ('completed','failed','cancelled','interrupted')),
  output_json TEXT NOT NULL CHECK(json_valid(output_json)),
  verified INTEGER NOT NULL CHECK(verified IN (0,1)),
  output_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
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
CREATE TABLE performance_spans (
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
CREATE INDEX idx_performance_spans_trace ON performance_spans(trace_id,created_at);
CREATE INDEX idx_performance_spans_name ON performance_spans(category,name,created_at);
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
CREATE INDEX idx_memories_scope ON memories(scope,workspace_key,session_id,status);
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
CREATE TABLE memory_workspace_settings (
  workspace_key TEXT PRIMARY KEY,
  write_enabled INTEGER NOT NULL DEFAULT 1 CHECK(write_enabled IN (0,1)),
  capture_enabled INTEGER NOT NULL DEFAULT 1 CHECK(capture_enabled IN (0,1)),
  manual_write_enabled INTEGER NOT NULL DEFAULT 1 CHECK(manual_write_enabled IN (0,1)),
  maintenance_enabled INTEGER NOT NULL DEFAULT 1 CHECK(maintenance_enabled IN (0,1)),
  policy TEXT NOT NULL DEFAULT 'automatic' CHECK(policy IN ('off','review','automatic')),
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
  envelope_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(envelope_json)),
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
CREATE INDEX idx_memory_candidates_queue ON memory_candidates(workspace_key,session_id,status,created_at);
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
CREATE TABLE memory_sources (
  id INTEGER PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  source_ref TEXT,
  extraction_id TEXT REFERENCES memory_extractions(id) ON DELETE SET NULL,
  workspace_key TEXT,
  session_id TEXT,
  run_id TEXT,
  message_id TEXT,
  evidence_id TEXT,
  quote TEXT,
  direct INTEGER NOT NULL DEFAULT 0 CHECK(direct IN (0,1)),
  verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
  valid INTEGER NOT NULL DEFAULT 1 CHECK(valid IN (0,1)),
  created_at TEXT NOT NULL,
  invalidated_at TEXT,
  UNIQUE(memory_id,source_kind,source_ref,extraction_id)
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
  cosine REAL,
  retrieval_score REAL NOT NULL DEFAULT 0,
  selected_reason TEXT,
  filter_reason TEXT,
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
