-- Captured agent lifecycle usage previously had no canonical llm_calls row.
-- Requeue only retained source evidence; normal normalization owns accounting
-- and durable downstream phase scope. Do not invent totals from session caches.
UPDATE raw_events SET normalized_status='pending', normalization_error=NULL
WHERE normalized_status='ok' AND id IN (
  SELECT raw.id FROM raw_events raw
  JOIN steps st ON st.id=reflect_stable_id('step',raw.id)
  WHERE st.type='agent_call'
    AND COALESCE(json_extract(raw.attrs_json,'$."reflect.usage.enrichment_only"'),0)=0
    AND (json_extract(raw.attrs_json,'$."gen_ai.usage.input_tokens"') IS NOT NULL
      OR json_extract(raw.attrs_json,'$."gen_ai.usage.output_tokens"') IS NOT NULL
      OR json_extract(raw.attrs_json,'$."gen_ai.usage.cache_read.input_tokens"') IS NOT NULL
      OR json_extract(raw.attrs_json,'$."gen_ai.usage.cache_creation.input_tokens"') IS NOT NULL
      OR json_extract(raw.attrs_json,'$."gen_ai.usage.reasoning_output_tokens"') IS NOT NULL
      OR json_extract(raw.attrs_json,'$."gen_ai.usage.cache_read_input_tokens"') IS NOT NULL
      OR json_extract(raw.attrs_json,'$."gen_ai.usage.cache_creation_input_tokens"') IS NOT NULL
      OR json_extract(raw.attrs_json,'$."hook.cache_read_tokens"') IS NOT NULL
      OR json_extract(raw.attrs_json,'$."hook.cache_write_tokens"') IS NOT NULL)
    AND NOT EXISTS (SELECT 1 FROM llm_calls lc WHERE lc.step_id=st.id)
);

-- The compact reader now distinguishes sample time from usage interval time.
DROP INDEX idx_llm_calls_measurement_facts;
CREATE INDEX idx_llm_calls_measurement_facts ON llm_calls(
  session_id,id,step_id,provider,request_model,response_model,input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens,reasoning_output_tokens,estimated_cost_usd,
  json_extract(raw_attrs_json,'$."reflect.time.provenance"','$."reflect.usage.time_provenance"','$."gen_ai.request.id"','$."reflect.usage.identity"','$."reflect.usage.input_semantics"','$."reflect.usage.grain"','$."reflect.token.source"','$."reflect.cost.source"','$."reflect.cost.breakdown"','$."reflect.model.provenance"','$."gen_ai.usage.input_tokens"','$."gen_ai.usage.output_tokens"','$."gen_ai.usage.cache_read.input_tokens"','$."hook.cache_read_tokens"','$."gen_ai.usage.cache_creation.input_tokens"','$."hook.cache_write_tokens"','$."gen_ai.request.mode"','$."gen_ai.client.requested_mode"','$."gen_ai.client.turn_id"','$."turn.id"','$."turn_id"','$."generation.id"','$."gen_ai.client.generation_id"')
);

-- Older usage-only passes could acknowledge the dirty session before Git
-- projection. Command/MCP classification belongs to the existing projector;
-- replay tool owners once rather than duplicate that classifier in SQL.
INSERT INTO pending_refresh(phase,session_id,revision,full_scope,deferred)
SELECT 'canonical',session_id,'projection-repair-v1',1,0
FROM tool_calls GROUP BY session_id
ON CONFLICT(phase,session_id) DO UPDATE SET
  revision=excluded.revision,full_scope=1,deferred=0;

-- Retained request-only observations also need the canonical identity rule.
-- Queue explicit duplicate identities only; reconciliation marks dependent
-- cost/rollup scope when the stored accounting buckets actually change.
INSERT INTO pending_refresh(phase,session_id,revision,full_scope,deferred)
SELECT 'canonical',session_id,'usage-identity-repair-v1',1,0
FROM (
  SELECT session_id,provider,
    COALESCE(NULLIF(json_extract(raw_attrs_json,'$."gen_ai.request.id"'),''),
             NULLIF(json_extract(raw_attrs_json,'$."reflect.usage.identity"'),'')) AS usage_identity
  FROM llm_calls
  GROUP BY session_id,provider,usage_identity
  HAVING usage_identity IS NOT NULL AND COUNT(*) > 1
) WHERE 1
ON CONFLICT(phase,session_id) DO UPDATE SET
  revision=excluded.revision,full_scope=1,deferred=0;

-- Source-only graph appends skipped memory node/exposure consumers. Expand
-- any existing scope so session repair restores all owned relationships.
INSERT INTO pending_refresh(phase,session_id,revision,full_scope,deferred)
SELECT 'graph',session_id,'projection-repair-v1',1,0
FROM (
  SELECT session_id FROM memories WHERE session_id IS NOT NULL
  UNION SELECT session_id FROM memory_exposures WHERE session_id IS NOT NULL
) WHERE 1
ON CONFLICT(phase,session_id) DO UPDATE SET
  revision=excluded.revision,full_scope=1,deferred=0;

-- Scope/chronology changes require fresh assessments, including retained
-- sessions outside the routine rolling analysis window. Keep source evidence.
INSERT INTO pending_refresh(phase,session_id,revision,deferred,full_scope)
SELECT DISTINCT 'improvements',eu.session_id,'execution-assessment-v2',0,1
FROM execution_units eu
WHERE json_extract(eu.boundary_json,'$.assessment.version') IS NULL
   OR json_extract(eu.boundary_json,'$.assessment.version') < 2
ON CONFLICT(phase,session_id) DO UPDATE SET
  revision=excluded.revision,deferred=0,full_scope=1;
UPDATE measurements SET cohort_json=json_set(cohort_json,'$.assessment_version',0);
