CREATE INDEX IF NOT EXISTS idx_execution_unit_steps_session
  ON execution_unit_steps(session_id);
CREATE INDEX IF NOT EXISTS idx_loop_occurrences_session
  ON loop_occurrences(session_id);
CREATE INDEX IF NOT EXISTS idx_skill_usage_session
  ON skill_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_workflow_exposures_session
  ON workflow_exposures(session_id);

CREATE INDEX IF NOT EXISTS idx_evidence_step
  ON evidence(step_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_step
  ON llm_calls(step_id);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_step
  ON mcp_calls(step_id);
CREATE INDEX IF NOT EXISTS idx_memories_step
  ON memories(step_id);
CREATE INDEX IF NOT EXISTS idx_observation_evidence_step
  ON observation_evidence(step_id);
CREATE INDEX IF NOT EXISTS idx_privacy_findings_step
  ON privacy_findings(step_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_step
  ON tool_calls(step_id);

CREATE INDEX IF NOT EXISTS idx_observation_evidence_llm_call
  ON observation_evidence(llm_call_id);
CREATE INDEX IF NOT EXISTS idx_observation_evidence_tool_call
  ON observation_evidence(tool_call_id);
