CREATE INDEX IF NOT EXISTS idx_tool_calls_input_fingerprint
  ON tool_calls(tool_name, input_hash, session_id)
  WHERE input_hash IS NOT NULL AND input_hash <> '';
