-- Keep windowed activity publication off large step payloads. The complete
-- json_object expression must match UsageService._scope_cte: separate indexed
-- json_extract expressions do not cover that nested projection in SQLite.
DROP INDEX idx_steps_measurement_times;
CREATE INDEX idx_steps_measurement_times ON steps(
  session_id, COALESCE(json_extract(raw_attrs_json,'$."reflect.time.provenance"'),'observed'),
  started_at, ended_at, id, type, summary,
  json_object('gen_ai.client.turn_id',json_extract(raw_attrs_json,'$."gen_ai.client.turn_id"'),
    'gen_ai.client.generation_id',json_extract(raw_attrs_json,'$."gen_ai.client.generation_id"'),
    'gen_ai.client.hook.event',json_extract(raw_attrs_json,'$."gen_ai.client.hook.event"'))
);
