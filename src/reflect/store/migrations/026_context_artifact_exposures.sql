CREATE TABLE memory_exposures (
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  step_id TEXT REFERENCES steps(id) ON DELETE SET NULL,
  content_hash TEXT,
  source TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (memory_id, session_id)
);

INSERT OR IGNORE INTO memory_exposures(
  memory_id, session_id, step_id, content_hash, source, observed_at,
  created_at, updated_at
)
SELECT
  id, session_id, step_id, content_hash, source,
  COALESCE(last_seen_at, created_at), created_at, updated_at
FROM memories
WHERE COALESCE(session_id, '') <> '';

CREATE INDEX idx_memory_exposures_session
  ON memory_exposures(session_id, memory_id);

CREATE INDEX idx_memory_exposures_step
  ON memory_exposures(step_id);

ALTER TABLE mcp_task_runs
  ADD COLUMN task_contract_id TEXT REFERENCES specs(id) ON DELETE SET NULL;

ALTER TABLE mcp_task_runs
  ADD COLUMN task_contract_hash TEXT;

ALTER TABLE specs
  ADD COLUMN content_hash TEXT;

CREATE INDEX idx_mcp_task_runs_contract_session
  ON mcp_task_runs(task_contract_id, runtime_session_id);
