ALTER TABLE mcp_task_runs ADD COLUMN runtime_identity_source TEXT;
ALTER TABLE mcp_task_runs ADD COLUMN runtime_identity_evidence_id TEXT;
ALTER TABLE mcp_task_runs ADD COLUMN runtime_identity_conflict TEXT;
UPDATE mcp_task_runs SET runtime_identity_source = 'environment'
WHERE runtime_session_id IS NOT NULL;

INSERT OR IGNORE INTO maintenance_tasks(task, requested_at)
VALUES ('reconcile_task_identities', CURRENT_TIMESTAMP);

-- Repair merged call evidence without replaying usage-bearing events.
CREATE TEMP TABLE inconsistent_tool_calls AS
SELECT tc.id, tc.step_id, tc.session_id FROM tool_calls tc
JOIN steps s ON s.id = tc.step_id
WHERE (tc.status = 'error' OR NULLIF(trim(tc.error_type), '') IS NOT NULL)
  AND (tc.status <> 'error' OR s.status <> 'error');
UPDATE tool_calls SET status = 'error', updated_at = CURRENT_TIMESTAMP
WHERE id IN (SELECT id FROM inconsistent_tool_calls);
UPDATE steps SET status = 'error', updated_at = CURRENT_TIMESTAMP
WHERE id IN (SELECT step_id FROM inconsistent_tool_calls);
DELETE FROM graph_nodes WHERE session_id IN (SELECT session_id FROM inconsistent_tool_calls);
INSERT OR IGNORE INTO maintenance_tasks(task, requested_at)
SELECT 'rebuild_rollups_after_tool_outcomes', CURRENT_TIMESTAMP
WHERE EXISTS (SELECT 1 FROM inconsistent_tool_calls);
DROP TABLE inconsistent_tool_calls;
