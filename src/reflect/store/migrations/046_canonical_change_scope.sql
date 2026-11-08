-- Keep precise canonical owners until dependent phases acknowledge their revision.
ALTER TABLE pending_refresh ADD COLUMN full_scope INTEGER NOT NULL DEFAULT 1
  CHECK(full_scope IN (0, 1));
CREATE TABLE pending_step_changes (
  phase TEXT NOT NULL,
  session_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  old_execution_unit_id TEXT,
  old_started_at TEXT,
  PRIMARY KEY(phase, session_id, step_id),
  FOREIGN KEY(phase, session_id) REFERENCES pending_refresh(phase, session_id) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE INDEX idx_steps_native_group ON steps(session_id,
  CAST(COALESCE(NULLIF(json_extract(raw_attrs_json,'$."gen_ai.client.turn_id"'),''),
                NULLIF(json_extract(raw_attrs_json,'$."turn.id"'),''),
                json_extract(raw_attrs_json,'$.turn_id')) AS TEXT), started_at, seq);
CREATE TABLE session_tool_rollups (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  tool_name TEXT NOT NULL,
  agent TEXT NOT NULL,
  call_count INTEGER NOT NULL,
  success_count INTEGER NOT NULL,
  error_count INTEGER NOT NULL,
  total_duration_ms INTEGER NOT NULL,
  PRIMARY KEY(session_id,tool_name,agent)
) WITHOUT ROWID;
CREATE INDEX idx_session_tool_rollup_key ON session_tool_rollups(tool_name,agent);
INSERT INTO session_tool_rollups
SELECT s.id,tc.tool_name,COALESCE(a.name,''),COUNT(*),
       SUM(CASE WHEN tc.status <> 'error' THEN 1 ELSE 0 END),
       SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END),COALESCE(SUM(tc.duration_ms),0)
FROM sessions s JOIN tool_calls tc ON tc.session_id=s.id
LEFT JOIN agents a ON a.id=s.agent_id GROUP BY s.id,tc.tool_name,COALESCE(a.name,'');
