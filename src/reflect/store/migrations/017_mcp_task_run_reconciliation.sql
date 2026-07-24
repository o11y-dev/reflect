ALTER TABLE mcp_task_runs ADD COLUMN session_linked_at TEXT;
ALTER TABLE mcp_task_runs ADD COLUMN session_outcome_recorded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mcp_task_runs ADD COLUMN skill_usage_recorded_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_mcp_task_runs_runtime_link
  ON mcp_task_runs(runtime_session_id, status, session_linked_at);
