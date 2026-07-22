CREATE TABLE IF NOT EXISTS session_task_archetypes (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  task_archetype_id TEXT NOT NULL REFERENCES task_archetypes(id) ON DELETE CASCADE,
  confidence REAL NOT NULL,
  features_json TEXT NOT NULL DEFAULT '{}',
  classified_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
  id TEXT PRIMARY KEY,
  workflow_version_id TEXT NOT NULL REFERENCES workflow_versions(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  input_json TEXT NOT NULL DEFAULT '{}',
  expected_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  result_json TEXT NOT NULL DEFAULT '{}',
  last_run_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (workflow_version_id, name)
);

CREATE INDEX IF NOT EXISTS idx_session_task_archetypes_archetype
  ON session_task_archetypes(task_archetype_id, session_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_workflow_status
  ON evaluations(workflow_version_id, status, updated_at DESC);
