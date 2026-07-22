CREATE TABLE IF NOT EXISTS loop_patterns (
  id TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
  tool_name TEXT,
  occurrence_count INTEGER NOT NULL DEFAULT 0,
  affected_session_count INTEGER NOT NULL DEFAULT 0,
  state_change_count INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'detected',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loop_occurrences (
  id TEXT PRIMARY KEY,
  loop_id TEXT NOT NULL REFERENCES loop_patterns(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  tool_name TEXT NOT NULL,
  input_hash TEXT,
  repeat_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  state_changed INTEGER NOT NULL DEFAULT 0,
  outcome TEXT,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (loop_id, session_id, input_hash)
);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  origin TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL DEFAULT 'pending',
  current_version_id TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  content_markdown TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  workflow_json TEXT NOT NULL DEFAULT '{}',
  source_kind TEXT NOT NULL,
  source_agent TEXT,
  source_loop_id TEXT REFERENCES loop_patterns(id) ON DELETE SET NULL,
  source_workflow_id TEXT,
  workflow_candidate_id TEXT REFERENCES workflow_candidates(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (skill_id, version),
  UNIQUE (skill_id, content_hash)
);

CREATE TABLE IF NOT EXISTS skill_evidence (
  id TEXT PRIMARY KEY,
  skill_version_id TEXT NOT NULL REFERENCES skill_versions(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  relationship TEXT NOT NULL DEFAULT 'supporting',
  confidence REAL NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE (skill_version_id, entity_type, entity_id, relationship)
);

CREATE TABLE IF NOT EXISTS skill_installations (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  skill_version_id TEXT REFERENCES skill_versions(id) ON DELETE SET NULL,
  target_kind TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  installed_hash TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_usage (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  skill_version_id TEXT REFERENCES skill_versions(id) ON DELETE SET NULL,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  outcome TEXT,
  confidence REAL NOT NULL DEFAULT 0,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (skill_id, session_id)
);

CREATE TABLE IF NOT EXISTS skill_measurements (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  skill_version_id TEXT REFERENCES skill_versions(id) ON DELETE SET NULL,
  metric_name TEXT NOT NULL,
  before_value REAL,
  after_value REAL,
  verdict TEXT NOT NULL DEFAULT 'insufficient_data',
  confidence REAL NOT NULL DEFAULT 0,
  measured_at TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_loop_patterns_status_confidence
  ON loop_patterns(status, confidence DESC, affected_session_count DESC);
CREATE INDEX IF NOT EXISTS idx_loop_patterns_repo_kind
  ON loop_patterns(repo_id, kind, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_loop_occurrences_loop_session
  ON loop_occurrences(loop_id, session_id, repeat_count DESC);
CREATE INDEX IF NOT EXISTS idx_skills_lifecycle_updated
  ON skills(lifecycle_state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_versions_skill_status
  ON skill_versions(skill_id, status, version DESC);
CREATE INDEX IF NOT EXISTS idx_skill_versions_candidate
  ON skill_versions(workflow_candidate_id, status);
CREATE INDEX IF NOT EXISTS idx_skill_evidence_entity
  ON skill_evidence(entity_type, entity_id, skill_version_id);
CREATE INDEX IF NOT EXISTS idx_skill_installations_skill_status
  ON skill_installations(skill_id, status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_usage_skill_observed
  ON skill_usage(skill_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_measurements_skill_measured
  ON skill_measurements(skill_id, measured_at DESC);
