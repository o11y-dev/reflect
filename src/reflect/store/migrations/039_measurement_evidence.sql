-- Derived measurements reuse canonical calls, execution units and evidence.
CREATE INDEX IF NOT EXISTS idx_evidence_kind_session ON evidence(kind, session_id);
DELETE FROM store_metadata WHERE key = 'cost_pricing_inputs_v1';
INSERT OR IGNORE INTO maintenance_tasks(task, requested_at)
SELECT 'rebuild_rollups_after_measurement_evidence', CURRENT_TIMESTAMP
WHERE EXISTS (SELECT 1 FROM sessions);

-- API usage completion is a request observation, not a completed agent turn.
UPDATE steps SET summary='Usage', raw_attrs_json=json_set(raw_attrs_json,
  '$."gen_ai.client.hook.event"','Usage', '$."reflect.turn.terminal"',json('false'),
  '$."reflect.usage.grain"','request')
WHERE lower(json_extract(raw_attrs_json,'$."gen_ai.client.hook.event"'))='stop'
  AND origin_kind='native_otlp_log'
  AND session_id IN (SELECT s.id FROM sessions s JOIN agents a ON a.id=s.agent_id
    WHERE a.name IN ('codex','claude'));
UPDATE llm_calls SET raw_attrs_json=json_set(raw_attrs_json,
  '$."gen_ai.client.hook.event"','Usage', '$."reflect.turn.terminal"',json('false'),
  '$."reflect.usage.grain"','request')
WHERE step_id IN (SELECT id FROM steps WHERE summary='Usage'
  AND origin_kind='native_otlp_log');

-- Preserve the original source/model attributes while making the historical
-- config fallback explicit to current pricing and measurement readers.
UPDATE steps SET raw_attrs_json=json_set(raw_attrs_json,
  '$."reflect.model.provenance"','config_default')
WHERE json_extract(raw_attrs_json,'$."gen_ai.request.model_source"')='codex_config_default';
UPDATE llm_calls SET raw_attrs_json=json_set(raw_attrs_json,
  '$."reflect.model.provenance"','config_default')
WHERE json_extract(raw_attrs_json,'$."gen_ai.request.model_source"')='codex_config_default'
  OR step_id IN (SELECT id FROM steps
    WHERE json_extract(raw_attrs_json,'$."gen_ai.request.model_source"')='codex_config_default');

-- Reports remain in mcp_task_runs; assessments are rebuilt from observations.
UPDATE execution_units SET outcome=NULL, verification_passed=NULL, eligible=0;
UPDATE measurements SET cohort_json=json_set(cohort_json,'$.assessment_version',0);

-- Cover compact measurement facts so interactive reads do not repeatedly parse
-- command/output blobs. SQLite maintains these with the canonical rows.
CREATE INDEX IF NOT EXISTS idx_steps_measurement_times ON steps(
  session_id, COALESCE(json_extract(raw_attrs_json,'$."reflect.time.provenance"'),'observed'),
  started_at, ended_at
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_measurement_facts ON tool_calls(
  session_id,logical_call_id,status,output_hash,
  json_extract(raw_attrs_json,'$."reflect.tool.identity_provenance"','$."gen_ai.client.hook.event"','$."process.exit.code"','$."gen_ai.client.turn_id"','$."turn.id"','$."turn_id"','$."generation.id"','$."gen_ai.client.generation_id"')
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_measurement_facts ON llm_calls(
  session_id,id,step_id,provider,request_model,response_model,input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens,reasoning_output_tokens,estimated_cost_usd,
  json_extract(raw_attrs_json,'$."reflect.time.provenance"','$."gen_ai.request.id"','$."reflect.usage.identity"','$."reflect.usage.input_semantics"','$."reflect.usage.grain"','$."reflect.token.source"','$."reflect.cost.source"','$."reflect.cost.breakdown"','$."reflect.model.provenance"','$."gen_ai.usage.input_tokens"','$."gen_ai.usage.output_tokens"','$."gen_ai.usage.cache_read.input_tokens"','$."hook.cache_read_tokens"','$."gen_ai.usage.cache_creation.input_tokens"','$."hook.cache_write_tokens"','$."gen_ai.request.mode"','$."gen_ai.client.requested_mode"','$."gen_ai.client.turn_id"','$."turn.id"','$."turn_id"','$."generation.id"','$."gen_ai.client.generation_id"')
);
