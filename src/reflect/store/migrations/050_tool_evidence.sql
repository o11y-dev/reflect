-- Materialize small command read projections once. Migration 50's Python data
-- repair uses the same tool/delegation/command extractors as normal ingestion;
-- it retains raw evidence and usage instead of replaying historical sources.
ALTER TABLE tool_calls ADD COLUMN command_template TEXT;
ALTER TABLE tool_calls ADD COLUMN is_shell_call INTEGER NOT NULL DEFAULT 0
  CHECK (is_shell_call IN (0, 1));
CREATE INDEX idx_tool_calls_command_projection
  ON tool_calls(session_id, is_shell_call, command_template);
