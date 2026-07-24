CREATE TABLE IF NOT EXISTS mcp_task_runs (
  id TEXT PRIMARY KEY,
  runtime_session_id TEXT,
  runtime_agent TEXT,
  workspace_path TEXT NOT NULL,
  task_file_path TEXT,
  question_hash TEXT NOT NULL,
  workflow_id TEXT,
  selected_skills_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'started',
  outcome TEXT,
  verification_passed INTEGER,
  completion_summary_redacted TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_task_runs_runtime_started
  ON mcp_task_runs(runtime_session_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_mcp_task_runs_status_started
  ON mcp_task_runs(status, started_at DESC);
