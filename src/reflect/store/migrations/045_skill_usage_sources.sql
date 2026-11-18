ALTER TABLE skill_usage RENAME TO skill_usage_aggregate_v1;

CREATE TABLE skill_usage (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  skill_version_id TEXT REFERENCES skill_versions(id) ON DELETE SET NULL,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  execution_unit_id TEXT REFERENCES execution_units(id) ON DELETE SET NULL,
  source_kind TEXT NOT NULL DEFAULT 'legacy',
  source_id TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL,
  outcome TEXT,
  confidence REAL NOT NULL DEFAULT 0,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(skill_id, source_kind, source_id)
);

-- Aggregate graph rows did not retain which occurrence, execution, or version
-- supported them. Keep their references as uncertain until source replay.
INSERT INTO skill_usage(
  id, skill_id, skill_version_id, session_id, execution_unit_id, source_kind,
  source_id, state, outcome, confidence, evidence_json, observed_at, created_at, updated_at
)
SELECT id, skill_id, NULL, session_id, NULL, 'legacy', id, 'inferred', NULL,
       0, json_object('source', 'legacy_aggregate', 'relationship', 'mentioned',
                      'attribution', 'source occurrence unavailable'),
       observed_at, created_at, updated_at
FROM skill_usage_aggregate_v1;
DROP TABLE skill_usage_aggregate_v1;

CREATE INDEX idx_skill_usage_execution ON skill_usage(execution_unit_id, skill_id);
CREATE INDEX idx_skill_usage_session ON skill_usage(session_id);
CREATE INDEX idx_skill_usage_skill_observed ON skill_usage(skill_id, observed_at DESC, id);
CREATE INDEX idx_skill_usage_skill_time ON skill_usage(skill_id, julianday(observed_at), id);
CREATE INDEX idx_skill_usage_source ON skill_usage(source_kind, source_id);

-- Explicit task reports are independently replayable claims.
UPDATE mcp_task_runs SET skill_usage_recorded_count=0
WHERE selected_skills_json <> '[]';

-- Historical impact synchronization selected the latest package version.
-- Refresh can reattach only a version matching the captured applied hash.
UPDATE skill_measurements SET skill_version_id=NULL
WHERE json_extract(details_json,'$.source_measurement_id') IS NOT NULL;
