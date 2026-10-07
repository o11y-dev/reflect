-- Reclassify retained result evidence without replaying usage-bearing events.
CREATE TEMP TABLE tool_failure_steps AS
SELECT id, session_id, reflect_tool_failure(summary, raw_attrs_json) AS failure
FROM steps
WHERE type IN ('tool_call', 'mcp_call', 'shell_command') AND status <> 'error';
DELETE FROM tool_failure_steps WHERE failure IS NULL;

CREATE TEMP TABLE tool_failure_calls AS
SELECT id, session_id, reflect_tool_failure('PostToolUse',
  json_set(raw_attrs_json,
    '$."gen_ai.client.hook.event"', 'PostToolUse',
    '$."gen_ai.client.tool.output"', output_preview_redacted
  )
) AS failure
FROM tool_calls
WHERE status <> 'error' AND output_preview_redacted IS NOT NULL;
DELETE FROM tool_failure_calls WHERE failure IS NULL;
CREATE UNIQUE INDEX tool_failure_calls_id ON tool_failure_calls(id);

CREATE TEMP TABLE tool_failure_sessions AS
SELECT session_id FROM tool_failure_steps
UNION SELECT session_id FROM tool_failure_calls;

UPDATE steps SET status = 'error', updated_at = CURRENT_TIMESTAMP
WHERE id IN (SELECT id FROM tool_failure_steps);

UPDATE tool_calls
SET status = 'error',
    error_type = COALESCE(error_type, (
      SELECT json_extract(failure, '$.type') FROM tool_failure_calls f WHERE f.id = tool_calls.id
    )),
    error_message_redacted = COALESCE(error_message_redacted, (
      SELECT json_extract(failure, '$.message') FROM tool_failure_calls f WHERE f.id = tool_calls.id
    )),
    updated_at = CURRENT_TIMESTAMP
WHERE id IN (SELECT id FROM tool_failure_calls);

UPDATE sessions
SET failure_count = (SELECT COUNT(*) FROM steps WHERE session_id = sessions.id AND status = 'error'),
    status = 'error', updated_at = CURRENT_TIMESTAMP
WHERE id IN (SELECT session_id FROM tool_failure_sessions);

-- Existing refresh/readiness machinery rebuilds derived data on explicit refresh.
DELETE FROM graph_nodes WHERE session_id IN (SELECT session_id FROM tool_failure_sessions);
INSERT OR IGNORE INTO maintenance_tasks(task, requested_at)
SELECT 'rebuild_rollups_after_tool_outcomes', CURRENT_TIMESTAMP
WHERE EXISTS (SELECT 1 FROM tool_failure_sessions);

DROP TABLE tool_failure_sessions;
DROP TABLE tool_failure_calls;
DROP TABLE tool_failure_steps;
