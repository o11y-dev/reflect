UPDATE mcp_calls AS target
SET status = (
      SELECT CASE
        WHEN MAX(CASE WHEN peer.status = 'error' THEN 1 ELSE 0 END) = 1 THEN 'error'
        WHEN MAX(CASE WHEN peer.status = 'ok' THEN 1 ELSE 0 END) = 1 THEN 'ok'
        ELSE 'unknown'
      END
      FROM mcp_calls AS peer
      WHERE peer.session_id = target.session_id
        AND peer.tool_call_id = target.tool_call_id
    ),
    duration_ms = (
      SELECT MAX(peer.duration_ms)
      FROM mcp_calls AS peer
      WHERE peer.session_id = target.session_id
        AND peer.tool_call_id = target.tool_call_id
    )
WHERE target.tool_call_id IS NOT NULL
  AND target.tool_call_id <> '';

DELETE FROM mcp_calls
WHERE tool_call_id IS NOT NULL
  AND tool_call_id <> ''
  AND rowid NOT IN (
    SELECT MIN(rowid)
    FROM mcp_calls
    WHERE tool_call_id IS NOT NULL AND tool_call_id <> ''
    GROUP BY session_id, tool_call_id
  );

DROP INDEX IF EXISTS idx_mcp_calls_session_tool_call;

CREATE UNIQUE INDEX idx_mcp_calls_session_tool_call
  ON mcp_calls(session_id, tool_call_id)
  WHERE tool_call_id IS NOT NULL AND tool_call_id <> '';

ALTER TABLE mcp_task_runs
  ADD COLUMN selected_memories_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE mcp_task_runs
  ADD COLUMN memory_exposure_recorded_count INTEGER NOT NULL DEFAULT 0;
