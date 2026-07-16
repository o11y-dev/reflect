CREATE TABLE IF NOT EXISTS rule_definitions (
  id TEXT NOT NULL,
  version INTEGER NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  detector_config_json TEXT NOT NULL DEFAULT '{}',
  required_signals_json TEXT NOT NULL DEFAULT '[]',
  lifecycle_state TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (id, version)
);

CREATE TABLE IF NOT EXISTS observations (
  id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  metric_name TEXT NOT NULL,
  metric_value REAL NOT NULL,
  metric_unit TEXT NOT NULL,
  metric_direction TEXT NOT NULL,
  baseline_value REAL,
  baseline_query_json TEXT NOT NULL DEFAULT '{}',
  impact_score REAL NOT NULL DEFAULT 0,
  severity TEXT NOT NULL,
  confidence REAL NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_evaluated_at TEXT NOT NULL,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  affected_session_count INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'new',
  suppression_reason TEXT,
  suppressed_until TEXT,
  actionability TEXT NOT NULL DEFAULT 'review',
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (rule_id, rule_version) REFERENCES rule_definitions(id, version),
  UNIQUE (rule_id, rule_version, scope_type, scope_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS observation_evidence (
  id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
  polarity TEXT NOT NULL DEFAULT 'supporting',
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  step_id TEXT REFERENCES steps(id) ON DELETE SET NULL,
  tool_call_id TEXT REFERENCES tool_calls(id) ON DELETE SET NULL,
  llm_call_id TEXT REFERENCES llm_calls(id) ON DELETE SET NULL,
  file_id TEXT REFERENCES files(id) ON DELETE SET NULL,
  memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
  summary_redacted TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1,
  attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE (observation_id, entity_type, entity_id, polarity)
);

CREATE TABLE IF NOT EXISTS session_outcomes (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  outcome TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  verification_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (session_id, source)
);

CREATE TABLE IF NOT EXISTS task_archetypes (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL,
  matching_features_json TEXT NOT NULL DEFAULT '{}',
  lifecycle_state TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_candidates (
  id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
  task_archetype_id TEXT REFERENCES task_archetypes(id) ON DELETE SET NULL,
  action_type TEXT NOT NULL,
  title TEXT NOT NULL,
  hypothesis TEXT NOT NULL,
  scope TEXT NOT NULL,
  risk TEXT NOT NULL DEFAULT 'low',
  content_json TEXT NOT NULL,
  support_count INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0,
  target_metric TEXT NOT NULL,
  target_value REAL,
  measurement_window INTEGER NOT NULL DEFAULT 10,
  status TEXT NOT NULL DEFAULT 'pending',
  checks_json TEXT NOT NULL DEFAULT '{}',
  provenance_json TEXT NOT NULL DEFAULT '{}',
  reviewer TEXT,
  reviewed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (observation_id, action_type)
);

CREATE TABLE IF NOT EXISTS workflow_versions (
  id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES workflow_candidates(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  content_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  render_targets_json TEXT NOT NULL DEFAULT '["skill"]',
  status TEXT NOT NULL DEFAULT 'approved',
  created_at TEXT NOT NULL,
  UNIQUE (candidate_id, version),
  UNIQUE (candidate_id, content_hash)
);

CREATE TABLE IF NOT EXISTS interventions (
  id TEXT PRIMARY KEY,
  workflow_version_id TEXT NOT NULL REFERENCES workflow_versions(id) ON DELETE RESTRICT,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  target_path TEXT NOT NULL,
  previous_hash TEXT,
  applied_hash TEXT NOT NULL,
  previous_content TEXT,
  applied_content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  exposure_started_at TEXT NOT NULL,
  rolled_back_at TEXT,
  rollback_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_exposures (
  id TEXT PRIMARY KEY,
  intervention_id TEXT NOT NULL REFERENCES interventions(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE (intervention_id, session_id)
);

CREATE TABLE IF NOT EXISTS measurements (
  id TEXT PRIMARY KEY,
  intervention_id TEXT NOT NULL REFERENCES interventions(id) ON DELETE CASCADE,
  metric_name TEXT NOT NULL,
  cohort_json TEXT NOT NULL,
  before_value REAL,
  after_value REAL,
  before_count INTEGER NOT NULL DEFAULT 0,
  after_count INTEGER NOT NULL DEFAULT 0,
  delta REAL,
  verdict TEXT NOT NULL DEFAULT 'insufficient_data',
  confidence REAL NOT NULL DEFAULT 0,
  confounders_json TEXT NOT NULL DEFAULT '[]',
  measured_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (intervention_id, metric_name, measured_at)
);

CREATE TABLE IF NOT EXISTS operator_feedback (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  outcome TEXT NOT NULL,
  reason_redacted TEXT,
  actor TEXT NOT NULL DEFAULT 'local_operator',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nudge_policies (
  id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  cooldown_seconds INTEGER NOT NULL DEFAULT 900,
  max_per_session INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (rule_id, rule_version) REFERENCES rule_definitions(id, version)
);

CREATE TABLE IF NOT EXISTS nudges (
  id TEXT PRIMARY KEY,
  policy_id TEXT NOT NULL REFERENCES nudge_policies(id) ON DELETE CASCADE,
  session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
  observation_id TEXT REFERENCES observations(id) ON DELETE SET NULL,
  message_redacted TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  delivered_at TEXT,
  acknowledged_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_bundle_imports (
  id TEXT PRIMARY KEY,
  bundle_version INTEGER NOT NULL,
  signer_id TEXT NOT NULL,
  signature TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE,
  redaction_policy_json TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS improvement_events (
  id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'reflect',
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_inbox
  ON observations(status, impact_score DESC, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_rule_scope
  ON observations(rule_id, rule_version, scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_observations_repo_status
  ON observations(repo_id, status, impact_score DESC);
CREATE INDEX IF NOT EXISTS idx_observation_evidence_observation
  ON observation_evidence(observation_id, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_observation_evidence_session
  ON observation_evidence(session_id, observation_id);
CREATE INDEX IF NOT EXISTS idx_workflow_candidates_status
  ON workflow_candidates(status, confidence DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_interventions_status
  ON interventions(status, exposure_started_at DESC);
CREATE INDEX IF NOT EXISTS idx_measurements_intervention
  ON measurements(intervention_id, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_operator_feedback_session
  ON operator_feedback(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_nudges_session_status
  ON nudges(session_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_improvement_events_entity
  ON improvement_events(entity_type, entity_id, created_at DESC);
