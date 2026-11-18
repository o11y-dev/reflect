-- Reassess only sessions whose existing execution boundaries change under the
-- shared harness-terminal rule. Keep old measurements and raw usage untouched.
-- Durable phase revisions survive interruption and bypass the seven-day window
-- once; ordinary late events subsequently use the existing incremental path.
WITH boundaries AS (
  SELECT eu.id, eu.session_id,
    eu.source,
    COALESCE(json_extract(eu.boundary_json, '$.assessment.boundary') = 'closed', 0) AS was_closed,
    MAX(reflect_event_time(COALESCE(NULLIF(st.ended_at, ''), st.started_at))) AS last_step,
    MAX(CASE WHEN reflect_observed_turn_terminal(json_extract(st.raw_attrs_json,
        '$."reflect.turn.terminal"', '$."gen_ai.client.hook.event"', '$."event.name"',
        '$."reflect.telemetry.origin"', '$."reflect.time.provenance"', '$."reflect.turn.identity_provenance"'))
        THEN reflect_event_time(COALESCE(NULLIF(st.ended_at, ''), st.started_at)) END) AS last_terminal,
    MAX(CASE WHEN reflect_event_time(COALESCE(NULLIF(st.ended_at, ''), st.started_at)) IS NULL
        OR COALESCE(json_extract(st.raw_attrs_json, '$."reflect.time.provenance"'), 'observed')
           IN ('synthetic', 'inferred', 'unavailable') THEN 1 ELSE 0 END) AS uncertain_time
  -- Read large step payloads sequentially, projecting only terminal facts into Python.
  FROM steps st NOT INDEXED
  CROSS JOIN execution_unit_steps eus ON eus.step_id = st.id
  CROSS JOIN execution_units eu ON eu.id = eus.execution_unit_id
  WHERE eu.eligible = 1 AND eu.source IN ('native_turn', 'prompt_fallback', 'session_fallback')
  GROUP BY eu.id
)
INSERT INTO pending_refresh(phase, session_id, revision, deferred)
SELECT DISTINCT 'improvements', session_id, 'terminal-facts-v2', 0
FROM boundaries
WHERE was_closed <> COALESCE(last_terminal = last_step AND uncertain_time = 0, 0)
   OR (was_closed = 1 AND source <> 'native_turn' AND EXISTS (
       SELECT 1 FROM execution_units peer
       WHERE peer.session_id = boundaries.session_id AND peer.source = 'native_turn' AND peer.eligible = 1
   ))
ON CONFLICT(phase, session_id) DO UPDATE SET revision=excluded.revision, deferred=0;
