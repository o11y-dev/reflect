CREATE TABLE execution_units (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  mcp_task_run_id TEXT UNIQUE REFERENCES mcp_task_runs(id) ON DELETE SET NULL,
  source TEXT NOT NULL,
  source_confidence REAL NOT NULL DEFAULT 0,
  workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
  repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
  agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',
  outcome TEXT,
  verification_passed INTEGER,
  eligible INTEGER NOT NULL DEFAULT 1,
  boundary_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE execution_unit_steps (
  execution_unit_id TEXT NOT NULL REFERENCES execution_units(id) ON DELETE CASCADE,
  step_id TEXT NOT NULL UNIQUE REFERENCES steps(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY (execution_unit_id, step_id)
);

CREATE TABLE execution_unit_archetypes (
  execution_unit_id TEXT PRIMARY KEY REFERENCES execution_units(id) ON DELETE CASCADE,
  task_archetype_id TEXT NOT NULL REFERENCES task_archetypes(id) ON DELETE CASCADE,
  confidence REAL NOT NULL,
  mixed INTEGER NOT NULL DEFAULT 0,
  features_json TEXT NOT NULL DEFAULT '{}',
  classified_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

ALTER TABLE mcp_task_runs ADD COLUMN execution_unit_id TEXT REFERENCES execution_units(id) ON DELETE SET NULL;

CREATE INDEX idx_execution_units_session_started
  ON execution_units(session_id, started_at, ended_at);
CREATE INDEX idx_execution_units_cohort
  ON execution_units(repo_id, workspace_id, eligible, started_at);
CREATE INDEX idx_execution_unit_steps_execution
  ON execution_unit_steps(execution_unit_id, step_id);
CREATE INDEX idx_execution_unit_archetypes_archetype
  ON execution_unit_archetypes(task_archetype_id, execution_unit_id);
CREATE INDEX idx_mcp_task_runs_execution_unit
  ON mcp_task_runs(execution_unit_id);

INSERT INTO execution_units(
  id, session_id, source, source_confidence, workspace_id, repo_id, agent_id,
  started_at, ended_at, status, eligible, boundary_json, created_at, updated_at
)
SELECT
  'execution_session_' || s.id,
  s.id,
  'session_fallback',
  0.5,
  s.workspace_id,
  s.repo_id,
  s.agent_id,
  s.started_at,
  s.ended_at,
  s.status,
  1,
  json_object('source', 'migration_session_fallback'),
  s.created_at,
  s.updated_at
FROM sessions s;

INSERT INTO execution_unit_steps(execution_unit_id, step_id, session_id, created_at)
SELECT 'execution_session_' || st.session_id, st.id, st.session_id, st.created_at
FROM steps st;

INSERT INTO execution_unit_archetypes(
  execution_unit_id, task_archetype_id, confidence, mixed, features_json,
  classified_at, updated_at
)
SELECT
  'execution_session_' || sta.session_id,
  sta.task_archetype_id,
  sta.confidence,
  0,
  sta.features_json,
  sta.classified_at,
  sta.updated_at
FROM session_task_archetypes sta;

ALTER TABLE workflow_exposures RENAME TO workflow_exposures_session_v1;

CREATE TABLE workflow_exposures (
  id TEXT PRIMARY KEY,
  intervention_id TEXT NOT NULL REFERENCES interventions(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  execution_unit_id TEXT NOT NULL REFERENCES execution_units(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE (intervention_id, execution_unit_id)
);

INSERT INTO workflow_exposures(
  id, intervention_id, session_id, execution_unit_id, state, evidence_json, created_at
)
SELECT
  id, intervention_id, session_id, 'execution_session_' || session_id,
  state, evidence_json, created_at
FROM workflow_exposures_session_v1;

DROP TABLE workflow_exposures_session_v1;

CREATE INDEX idx_workflow_exposures_execution
  ON workflow_exposures(execution_unit_id, intervention_id);

ALTER TABLE skill_usage RENAME TO skill_usage_session_v1;

CREATE TABLE skill_usage (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  skill_version_id TEXT REFERENCES skill_versions(id) ON DELETE SET NULL,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  execution_unit_id TEXT NOT NULL REFERENCES execution_units(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  outcome TEXT,
  confidence REAL NOT NULL DEFAULT 0,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (skill_id, execution_unit_id)
);

INSERT INTO skill_usage(
  id, skill_id, skill_version_id, session_id, execution_unit_id, state, outcome,
  confidence, evidence_json, observed_at, created_at, updated_at
)
SELECT
  id, skill_id, skill_version_id, session_id, 'execution_session_' || session_id,
  state, outcome, confidence, evidence_json, observed_at, created_at, updated_at
FROM skill_usage_session_v1;

DROP TABLE skill_usage_session_v1;

CREATE INDEX idx_skill_usage_execution
  ON skill_usage(execution_unit_id, skill_id);
