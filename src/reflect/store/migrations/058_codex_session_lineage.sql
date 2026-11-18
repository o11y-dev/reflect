-- Keep periodic lineage repair scoped to explicit native Codex spawn records.
CREATE INDEX idx_steps_codex_spawn_lineage ON steps(
  session_id, json_extract(raw_attrs_json, '$."source.subagent.thread_spawn.parent_thread_id"')
)
WHERE origin_kind = 'native_session' AND CASE WHEN json_valid(raw_attrs_json) THEN
  json_type(raw_attrs_json, '$."source.subagent.thread_spawn.parent_thread_id"') = 'text'
END;
